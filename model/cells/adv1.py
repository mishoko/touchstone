#!/usr/bin/env python3
"""ADV-1: a needle position for leverage, under width = W.

A needle is a narrow range (one tick spacing up to a few hundred ticks) holding the vault's inventory. Its liquidity
per unit of NAV is many times that of a W-wide range, so any pay or record that scales with liquidity × in-range
time would be levered by it.

Touchstone rules exercised: a placement's width must equal W exactly (`placement_check` → Width), there is one
position, and the base fee scales with δ = deployed value / NAV ≤ 1, not with liquidity.

Per state (in-range and exit states of an edge60-managed path; market = reference = spot):
The assertions are structural: they hold by construction of the rules (the width code of the placement check, the
δ ≤ 1 cap) and the cell verifies that the rules reject or bound every needle on real states, not a measured net.
- Assertion (all three, every row):
  1. every aligned range of width w ∈ {10, 20, 60, 100, 200, 500, 1000, 1990, 2010, 4000} containing spot and the
     reference is rejected with the width error;
  2. every legal W-wide range (a 40-tick grid plus both edges) has base leverage δ_after ≤ 1 (pay per credited block
     ≤ m × NAV);
  3. the disclosed exposure of every legal range, β = L / L_centred (L_centred = the centred W range holding the same
     inventory value), is ≤ β_W (the maximum a W-wide range can reach, about 1.07), i.e. the record cannot be levered.
- Control (both defences off: no width rule AND a pay that scales with exposure L × in-range time, the form the
  needle attacks): a causal needle (20 ticks centred on spot, chosen before the path is seen) held for one hour on a
  1-minute GBM continuation; its effective exposure β_eff = (L_needle × in-range minutes) / (L_centred × in-range
  minutes of the centred range) and the curator's net gain (β_eff − 1) × m × NAV × 1 h at m = 1.05%/yr must have
  mean > 0 and a one-sided 95% CI lower bound > 0 in every σ bucket. The best needle with hindsight is reported only.
"""
import json
import math
import multiprocessing as mp
import os
import random
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from common import R, sqrt_at, SIGMAS, TEST_SEEDS, W, SPACING, floor_sp, sample_state, mean_ci  # noqa: E402

WIDTHS = (10, 20, 60, 100, 200, 500, 1000, 1990, 2010, 4000)
OUT = os.path.join(os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))), "output", "g0")


def inventory(h, r):
    x, y = R.position_amounts(h.L, h.lo, h.hi, sqrt_at(r)) if h.L else (0, 0)
    return R.Holdings(h.idle0 + x, h.idle1 + y)


def l_of(inv, lo, hi, s):
    sa, sb = sqrt_at(lo), sqrt_at(hi)
    return R.liquidity_for_amounts_sat(s, sa, sb, inv.idle0, inv.idle1)


def l_centred(nav, s, r):
    """The liquidity of the centred W range holding inventory value `nav` at sqrt price s (the balanced split)."""
    lo = floor_sp(r) - W // 2
    sa, sb = sqrt_at(lo), sqrt_at(lo + W)
    # value per unit L at s: x(s)·p + y(s) for L = 2^96 units, then scale
    unit = 1 << 96
    x, y = R.get_amount0_delta(s, sb, unit, False), R.get_amount1_delta(sa, s, unit, False)
    per = R.q(x, s) + y
    return nav * unit // per, lo


def minute_path(seed, sigma, r, minutes=60):
    rng = random.Random(seed * 7919 + int(sigma * 1000))
    sd = sigma * math.sqrt(60 / (365 * 86400)) / math.log(1.0001)
    t, out = float(r), []
    for _ in range(minutes):
        t += rng.gauss(0.0, sd)
        out.append(math.floor(t))
    return out


def run_path(args):
    seed, sigma = args
    rows = []
    for state in ("in", "exit"):
        h, r, _ = sample_state(seed, sigma, exit_state=(state == "exit"))
        s = sqrt_at(r)
        inv = inventory(h, r)
        nav = R.value(inv, s)[0]
        lc, clo = l_centred(nav, s, r)
        # 1. non-W widths are rejected
        width_ok = True
        for w in WIDTHS:
            for lo in range(floor_sp(r) - w + SPACING, floor_sp(r) + SPACING, max(SPACING, w // 4 // SPACING * SPACING)):
                if lo % SPACING or not (lo <= r < lo + w):
                    continue
                if R.placement_check(lo, lo + w, r, r, 60, W, SPACING) != 2:
                    width_ok = False
        # 2-3. legal W ranges: δ and exposure
        lo_min, lo_max = floor_sp(r) + SPACING - W, floor_sp(r)
        best_delta, best_beta = 0, 0.0
        for lo in sorted(set(list(range(lo_min, lo_max + 1, 40)) + [lo_min, lo_max])):
            if R.placement_check(lo, lo + W, r, r, 60, W, SPACING):
                continue
            L, after, dl = R.placement_size(inv, lo, lo + W, s, s)
            if L == 0:
                continue
            best_delta = max(best_delta, dl)
            best_beta = max(best_beta, L / lc)
        # the control: the width rule off, the best needle by effective exposure over the next hour
        mp_ = minute_path(seed, sigma, r)
        cen_in = sum(1 for t in mp_ if clo <= t < clo + W)
        best_eff = 0.0
        for w in (10, 20, 60, 100, 200):
            for lo in range(floor_sp(r) - w + SPACING, floor_sp(r) + SPACING, SPACING):
                if not (lo <= r < lo + w):
                    continue
                L = l_of(inv, lo, lo + w, s)
                mins = sum(1 for t in mp_ if lo <= t < lo + w)
                if cen_in:
                    best_eff = max(best_eff, (L * mins) / (lc * cen_in))
        nlo = floor_sp(r) - 10
        L_n = l_of(inv, nlo, nlo + 20, s)
        mins_n = sum(1 for t in mp_ if nlo <= t < nlo + 20)
        causal = (L_n * mins_n) / (lc * cen_in) if cen_in else 0.0
        net_ctl = (causal - 1) * 0.0105 * nav / (365 * 24)   # wei over one hour under an exposure-scaled pay
        rows.append(dict(seed=seed, sigma=sigma, state=state, width_rule_ok=width_ok, max_delta=best_delta,
                         max_beta_W=best_beta, ctl_beta_eff=best_eff, ctl_beta_causal=causal, ctl_net_wei=net_ctl,
                         nav=nav))
    return rows


def summarize(rows):
    ok_w = all(r["width_rule_ok"] for r in rows)
    md = max(r["max_delta"] for r in rows)
    mb = max(r["max_beta_W"] for r in rows)
    ok = ok_w and md <= R.WAD and mb <= 1.08
    res = dict(rows=len(rows), width_rule_rejects_all=ok_w, max_delta=md / R.WAD, max_beta_W=mb, control={})
    print(f"ADV-1: {len(rows)} states; every non-W width rejected: {ok_w}; max δ_after {md / R.WAD:.6f}; "
          f"max exposure β over legal W ranges {mb:.4f} (bound 1.08)")
    for sg in SIGMAS:
        g = [r for r in rows if r["sigma"] == sg]
        hind = mean_ci([r["ctl_beta_eff"] for r in g])
        cb = mean_ci([r["ctl_beta_causal"] for r in g])
        cn = mean_ci([r["ctl_net_wei"] / 1e18 for r in g])
        res["control"][str(sg)] = dict(n=len(g), beta_causal_mean=cb[0], beta_causal_ci_lo=cb[1], net_eth_mean=cn[0],
                                       net_eth_ci_lo=cn[1], beta_hindsight_mean=hind[0])
        print(f"  control (width rule off + exposure pay; causal 20-tick needle) σ {sg}: β_eff mean {cb[0]:.1f}× "
              f"(CI lo {cb[1]:.1f}×), net {cn[0]:.4f} ETH per hour (CI lo {cn[1]:.4f}); hindsight best needle "
              f"{hind[0]:.1f}× (reported)")
        ok &= cn[1] > 0
    res["ok"] = ok
    print("ADV-1 verdict:", "PASS" if ok else "FAIL")
    return res


def main():
    seeds = list(TEST_SEEDS)[: int(sys.argv[1])] if len(sys.argv) > 1 else list(TEST_SEEDS)
    jobs = [(s, sg) for sg in SIGMAS for s in seeds]
    t0 = time.time()
    rows = []
    with mp.Pool(9) as pool:
        for i, r in enumerate(pool.imap_unordered(run_path, jobs, chunksize=4)):
            rows.extend(r)
            if (i + 1) % 200 == 0:
                print(f"  {i + 1}/{len(jobs)} paths, {time.time() - t0:.0f}s", flush=True)
    os.makedirs(OUT, exist_ok=True)
    with open(os.path.join(OUT, "adv1.json"), "w") as fh:
        json.dump(rows, fh)
    res = summarize(rows)
    with open(os.path.join(OUT, "adv1-summary.json"), "w") as fh:
        json.dump(res, fh, indent=1, default=str)
    return res["ok"]


if __name__ == "__main__":
    sys.exit(0 if main() else 1)
