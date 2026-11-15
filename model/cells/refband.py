#!/usr/bin/env python3
"""REF_BAND calibration (spec: the smallest of {20, 60, 100} with action blocking ≤ 1% at σ p90).

Action blocking = the share of time in which a curator action fails the fence |tick_spot − refTick| > REF_BAND.
- Market: 1-minute log-price paths in ticks, 30 days; driftless GBM at σ 40 / 60 / 120 %/yr (the p50 / p90 /
  p99 buckets) and, as a fat-tail stress, Student-t (ν = 3) increments with the same variance.
- refTick: the floor of the 10-minute TWAP (trapezoid over the 1-minute points).
- tick_spot: the pool tick tracks the market through arbitrage up to the fee (FEE_TICKS = 5): measured both as
  the market tick itself (central) and as the market displaced FEE_TICKS away from the reference (conservative:
  the pool stops up to one fee short of the market, on the worse side).
- Seeds 0–99 select the band (fit); seeds 100–299 confirm it (test). Reported per σ: the blocking share for
  each band, and its p99 over paths.
The rule is evaluated at σ 60 % (p90) on the conservative measure.
"""
import json
import math
import os
import sys

import numpy as np

HERE = os.path.dirname(os.path.abspath(__file__))
OUT = os.path.join(os.path.dirname(os.path.dirname(HERE)), "output", "g0")
BANDS = (20, 60, 100)
SIGMAS = (0.40, 0.60, 1.20)
FEE_TICKS = 5
DAYS = 30
WIN = 10  # minutes


def paths(seeds, sigma, kind="gbm"):
    n = DAYS * 1440 + WIN
    sd = sigma * math.sqrt(60 / (365 * 86400)) / math.log(1.0001)
    out = np.empty((len(seeds), n))
    for i, s in enumerate(seeds):
        rng = np.random.default_rng(1_000_003 * s + int(sigma * 1000) + (7 if kind == "t3" else 0))
        if kind == "gbm":
            z = rng.standard_normal(n)
        else:
            z = rng.standard_t(3, n) / math.sqrt(3.0)
        out[i] = np.cumsum(sd * z)
    return out


def blocking(x):
    """x: (paths, minutes) market ticks. Returns per-band arrays of per-path blocking shares (central, conservative)."""
    k = WIN
    cs = np.cumsum(x, axis=1)
    # trapezoid average over the window [t-k, t]: (sum of k+1 points − half the two ends) / k
    tot = cs[:, k:] - np.concatenate([np.zeros((x.shape[0], 1)), cs[:, :-k - 1]], axis=1)
    avg = (tot - 0.5 * (x[:, :-k] + x[:, k:])) / k
    ref = np.floor(avg)
    spot = np.round(x[:, k:])
    gap = np.abs(spot - ref)
    res = {}
    for b in BANDS:
        res[b] = ((gap > b).mean(axis=1), (gap + FEE_TICKS > b).mean(axis=1))
    return res, gap


def main():
    os.makedirs(OUT, exist_ok=True)
    table = {}
    for phase, seeds in (("fit", range(0, 100)), ("test", range(100, 300))):
        for kind in ("gbm", "t3"):
            for sigma in SIGMAS:
                res, gap = blocking(paths(list(seeds), sigma, kind))
                key = f"{phase}-{kind}-{int(sigma * 100)}"
                table[key] = {str(b): dict(central=float(c.mean()), conservative=float(v.mean()),
                                           conservative_p99=float(np.quantile(v, 0.99)))
                              for b, (c, v) in res.items()}
                table[key]["lag_p50_p99_max"] = [float(np.quantile(gap, 0.5)), float(np.quantile(gap, 0.99)),
                                                 float(gap.max())]
    sel = {}
    for phase in ("fit", "test"):
        ok = [b for b in BANDS if table[f"{phase}-gbm-60"][str(b)]["conservative"] <= 0.01]
        sel[phase] = min(ok) if ok else None
    table["selected"] = sel
    with open(os.path.join(OUT, "refband.json"), "w") as fh:
        json.dump(table, fh, indent=1)
    for k, v in table.items():
        if k == "selected":
            continue
        print(k, " ".join(f"b{b}: {v[str(b)]['central'] * 100:.3f}%/{v[str(b)]['conservative'] * 100:.3f}%"
                          f" (p99 {v[str(b)]['conservative_p99'] * 100:.2f}%)" for b in BANDS),
              "|gap| p50/p99/max", [round(x, 1) for x in v["lag_p50_p99_max"]])
    print("selected (rule at σ 60%, conservative):", sel)
    return sel


if __name__ == "__main__":
    sys.exit(0 if main()["test"] is not None else 1)
