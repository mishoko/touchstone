#!/usr/bin/env python3
"""Cell (e): the trend bias of the worse-of charge on an honest curator (registry finding F-014).

The charge prices every action at the worse of spot and the 10-minute reference TWAP. In a trend the TWAP lags the
market on one side, so a rebalance that changes the inventory mix shows a loss at one of the two prices: the honest
curator pays a "trend tax" exactly when re-ranging matters most.

Setup: the full vault model at the mainnet tier (12 s blocks, GRID 1 h, EPOCH 7 d, CURE 24 h, V 4 epochs), the
reference policy edge60, 180-day paths: driftless GBM at σ ∈ {40, 60, 120}%/yr plus a deterministic log-drift
μ ∈ {−100, −50, 0, +50, +100}%/yr; third-party depth 0 with modelled arbitrage; NAV 1,000 WETH; test seeds 100–299;
m = 1.05%/yr (the published method's value on the fit seeds) and the control m = 0.

Measured (reported): edge60's charges and actions per year by drift; the trend bias = charges(μ) − charges(0),
paired by seed, with a one-sided 95% CI.
Assertion (the net-pay floor): edge60's p5 net ≥ 0 at σ 60% for every drift at m = 1.05%/yr.
Control: at m = 0 the same p5 net is < 0 for every drift (the floor is not met without the base fee, so the
assertion is not vacuous).
"""
import json
import math
import multiprocessing as mp
import os
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import sim as S  # noqa: E402
import bots as B  # noqa: E402
from common import T0, mean_ci, pctl  # noqa: E402

DAYS = 180
SIGMAS = (0.40, 0.60, 1.20)
MUS = (-1.0, -0.5, 0.0, 0.5, 1.0)
M_METHOD = 0.0105
OUT = os.path.join(os.path.dirname(os.path.dirname(HERE)), "output", "g0")


def drifted_path(seed, sigma, mu):
    path = S.market_path(seed, sigma, DAYS, "gbm")
    per_step = mu * S.STEP_SEC / (365 * 86400) / math.log(1.0001)
    return [x if i < 2 else x + (i - 1) * per_step for i, x in enumerate(path)]


def run_job(args):
    seed, sigma, mu = args
    path = drifted_path(seed, sigma, mu)
    rows = []
    for m in (0.0, M_METHOD):
        sim = S.Sim(path, m, observer=False)
        r = sim.run(B.make("edge60"), DAYS * 24)
        rows.append(dict(seed=seed, sigma=sigma, mu=mu, m=m, net=r["net"], charges=r["charges"], actions=r["actions"],
                         gas=r["gas"], f=r["f"], breaches=r["breaches"], nav_end=r["nav_end"]))
    return rows


def summarize(rows):
    yrs = DAYS / 365
    ok = True
    res = {}
    ix = {(r["seed"], r["sigma"], r["mu"], r["m"]): r for r in rows}
    seeds = sorted({r["seed"] for r in rows})
    print("Cell (e) F-014 trend bias (edge60, m = 1.05%/yr; ETH per year on 1,000 WETH unless stated)")
    for sg in SIGMAS:
        for mu in MUS:
            g = [ix[(s, sg, mu, M_METHOD)] for s in seeds]
            g0 = [ix[(s, sg, mu, 0.0)] for s in seeds]
            ch = [x["charges"] / yrs for x in g]
            bias = [(ix[(s, sg, mu, M_METHOD)]["charges"] - ix[(s, sg, 0.0, M_METHOD)]["charges"]) / yrs for s in seeds]
            bm, blo, bhi = mean_ci(bias)
            p5 = pctl([x["net"] for x in g], 0.05)
            p5_0 = pctl([x["net"] for x in g0], 0.05)
            key = f"{sg}/{mu}"
            res[key] = dict(charges_yr=sum(ch) / len(ch), actions_yr=sum(x["actions"] for x in g) / len(g) / yrs,
                            bias_yr=bm, bias_ci=(blo, bhi), p5_net=p5, p5_net_m0=p5_0,
                            mean_net=sum(x["net"] for x in g) / len(g), breached=sum(1 for x in g if x["breaches"]))
            print(f"  σ {sg:.2f} μ {mu:+.1f}: charges {res[key]['charges_yr']:.3f}/yr ({res[key]['actions_yr']:.0f} actions), "
                  f"bias {bm:+.3f} (CI {blo:+.3f}..{bhi:+.3f}), net p5 {p5:+.3f} / mean {res[key]['mean_net']:+.3f} ETH per 180 d; "
                  f"m = 0: p5 {p5_0:+.3f}; breached paths {res[key]['breached']}")
            if sg == 0.60:
                ok &= p5 >= 0 and p5_0 < 0
    res["ok"] = ok
    print("Cell (e) verdict:", "PASS" if ok else "FAIL")
    return res


def main():
    seeds = list(range(100, 100 + (int(sys.argv[1]) if len(sys.argv) > 1 else 200)))
    jobs = [(s, sg, mu) for sg in SIGMAS for mu in MUS for s in seeds]
    t0 = time.time()
    rows = []
    with mp.Pool(9) as pool:
        for i, r in enumerate(pool.imap_unordered(run_job, jobs, chunksize=2)):
            rows.extend(r)
            if (i + 1) % 300 == 0:
                print(f"  {i + 1}/{len(jobs)} paths, {time.time() - t0:.0f}s", flush=True)
    os.makedirs(OUT, exist_ok=True)
    with open(os.path.join(OUT, "trend.json"), "w") as fh:
        json.dump(rows, fh)
    res = summarize(rows)
    with open(os.path.join(OUT, "trend-summary.json"), "w") as fh:
        json.dump(res, fh, indent=1, default=str)
    return res["ok"]


if __name__ == "__main__":
    sys.exit(0 if main() else 1)
