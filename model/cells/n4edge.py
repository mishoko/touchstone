#!/usr/bin/env python3
"""Cell (g): N4-edge, a self-sandwich around a no-swap EDGE placement (the reference policy's placement shape).

Coalition = curator + Sybil. From a path state (in range, or the first hour out of range), the coalition moves
spot to one unit inside an edge of a legal W-wide range (the bundled move of the reference policy: one unit above
sqrtAt(lo), so the pool tick equals lo, on the token0 side; sqrtAt(hi) − 1 on the token1 side), at a distance
d ∈ {0, 20, 30, 100} ticks from the market price r (0 = the honest placement), the curator re-places the vault on
that range with no pre-swap, and the Sybil swaps the price back to r. The range must contain spot and the
reference (= r) and pass the fence (REF_BAND 60; 100 for d = 100) and DELTA_MIN; third-party depth k ∈ {0, 1}.
Coalition accounting, the φ-scaled charge and the control formulation are cell (d)'s (spec decision log: the
optimal attacker's expected profit at k = 0 under a φ = 10% charge, per path the best over states, sides and
pushes, or abstaining).
Rule: the maximum net over every path, state, side, push and depth at φ = 100% ≤ 1 wei per action; the control
> 0 with a one-sided 95% CI lower bound > 0 in every σ bucket.
"""
import json
import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from common import R, sqrt_at, SIGMAS, TEST_SEEDS, W, SPACING, floor_sp, sample_state, mean_ci, pctl  # noqa
from n4 import run_attack, coalition_net, PHIS, F_CAP  # noqa

DS = (0, 20, 30, 100)
KS = (0, 1)


def edge_targets(r, d):
    """(lo, push sqrt, side) for the token0 edge at r − d and the token1 edge at r + d."""
    lo0 = floor_sp(r - d)
    hi1 = floor_sp(r + d) + SPACING
    return [(lo0, sqrt_at(lo0) + 1, "t0"), (hi1 - W, sqrt_at(hi1) - 1, "t1")]


def best_for_state(h, r, d, k):
    band = 60 if d <= 60 else 100
    best = {}
    for lo, ps, side in edge_targets(r, d):
        a = run_attack(h, r, r, 0, lo, k, band, push_sqrt=ps)
        if not a:
            continue
        for phi in PHIS:
            for f in (F_CAP, 0):
                n = coalition_net(a, phi, f)
                if (phi, f) not in best or n > best[(phi, f)][0]:
                    best[(phi, f)] = (n, a["nav0"], a["charge"], side)
    return best


def main(quick=False):
    seeds = list(TEST_SEEDS)[: (20 if quick else len(TEST_SEEDS))]
    t0 = time.time()
    rows = []
    for sigma in SIGMAS:
        for seed in seeds:
            for variant in ("in", "exit"):
                h, r, _ = sample_state(seed, sigma, exit_state=(variant == "exit"))
                for d in DS:
                    for k in KS:
                        b = best_for_state(h, r, d, k)
                        if not b:
                            continue
                        rows.append(dict(sigma=sigma, seed=seed, variant=variant, d=d, k=k, side=b[(R.WAD, F_CAP)][3],
                                         **{f"net_{p}_{f}": b[(p, f)][0] for p in PHIS for f in (F_CAP, 0)},
                                         nav=b[(R.WAD, F_CAP)][1], charge=b[(R.WAD, F_CAP)][2]))
        print(f"  sigma {sigma}: {len(rows)} rows, {time.time() - t0:.0f}s", flush=True)
    out = os.path.join(os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))), "output", "g0")
    os.makedirs(out, exist_ok=True)
    with open(os.path.join(out, "n4edge.json"), "w") as fh:
        json.dump(rows, fh)
    return summarize(rows, seeds)


def summarize(rows, seeds):
    full = f"net_{R.WAD}_{F_CAP}"
    res, ok_all = {}, True
    print("\nN4-edge (cell g); nets in bp of NAV")
    for d in DS:
        g = [r for r in rows if r["d"] == d]
        if not g:
            continue
        mx = max(max(r[full], r[f"net_{R.WAD}_0"]) for r in g)
        nets = [r[full] * 1e4 / r["nav"] for r in g]
        res[d] = dict(rows=len(g), max_net_wei=mx, max_net_bp=max(nets), p50_net_bp=pctl(nets, 0.5),
                      max_charge_bp=max(r["charge"] * 1e4 / r["nav"] for r in g), assert_ok=mx <= 1)
        ok_all &= mx <= 1
        print(f"  d={d:3d}: rows {len(g)}, max net {mx} wei ({max(nets):.4f} bp), max charge "
              f"{res[d]['max_charge_bp']:.4f} bp -> {'PASS' if mx <= 1 else 'FAIL'}")
    ctl = {}
    for sigma in SIGMAS:
        for phi, name in ((R.WAD // 10, "phi10"), (0, "off")):
            best = {s: 0.0 for s in seeds}
            for r in rows:
                if r["sigma"] == sigma and r["k"] == 0 and r["d"] > 0:
                    best[r["seed"]] = max(best[r["seed"]], max(0.0, r[f"net_{phi}_{F_CAP}"] * 1e4 / r["nav"]))
            m, lo, _ = mean_ci(list(best.values()))
            ctl[f"{name}@{sigma}"] = dict(n=len(best), mean_bp=m, ci_lo_bp=lo, profitable=sum(1 for x in best.values() if x > 0))
            if name == "phi10":
                ok_all &= lo > 0
            print(f"      {name}@{sigma}: mean {m:.4f} bp, CI lo {lo:.4f}, profitable paths {ctl[f'{name}@{sigma}']['profitable']}")
    res["control"] = ctl
    res["ok"] = ok_all
    print("N4-edge verdict:", "PASS" if ok_all else "FAIL")
    return res


if __name__ == "__main__":
    r = main(quick="--quick" in sys.argv)
    sys.exit(0 if r["ok"] else 1)
