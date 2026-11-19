#!/usr/bin/env python3
"""U-IC2 pre-registration checks (s24 panel 2026-11-19-s24-fix-uic-e0, decision D2), on spent seeds only.

For a candidate mimic (edge-12h) and the current one (edge-6h), at m = 2.2%/yr, in every `incentives.KINDS` bucket
and both observer environments, over the spent seeds 100-699 (fit data since s24; never seeds 700+):
- breaches: the number of paths with any breach (must be 0 for the mimic);
- the CURE p99 rule (spec §10, §3.11): CURE_BLOCKS >= p99 over paths of the mimic's maximum observed out-of-range
  clock `outBlocks`, with a margin of at least 3·GRID (D2).

The maximum is read after every `snapshot` call, the only place the clock grows (`reference.py` Hook.snapshot; a
reference refresh goes through it). The simulation is `regate_l3.job`'s, unchanged: same path, same Sim arguments,
same RNG key, the frozen fee-aware settlement with integer closes.

D2's third item (s24 design review S24U-05), with `--l8`: L8 (the mimic at m = 1%/yr with the eroding
`BOND_RATIO`·S bond, the gate enforced, no top-up; `regate_l3.job`'s L8 call verbatim) and the honest-cost row (the
mimic at m = 2.2%/yr, no observer, a bond that never binds), for edge-12h and edge-6h per bucket.

Usage: python3 model/cells/uic2_precheck.py [procs] [first] [n] [--l8]   (default 4 workers, seeds 100-699)
Writes output/uic2/precheck.json and prints the table; exit 1 if the edge-12h rule fails.
"""
import json
import multiprocessing as mp
import os
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import sim as S  # noqa: E402
import bots as B  # noqa: E402
import incentives as I  # noqa: E402
import regate_l3 as R  # noqa: E402
from common import T0  # noqa: E402

M = 0.022
CANDIDATES = ("edge-12h", "edge-6h")
ROOT = os.path.dirname(os.path.dirname(HERE))
OUT = os.path.join(ROOT, "output", "uic2")
SPENT = range(100, 700)


def max_out_run(seed, kind, sigma, name, obs):
    S.SETTLE["rule"] = R.RULE
    S.SETTLE["int_closes"] = R.INT_CLOSES
    path = S.market_path(seed, sigma, I.DAYS, kind, edge=T0 + 1000)
    sim = S.Sim(path, M, observer=obs, place=B.PLACES.get(name, True), sigma=sigma,
                rng_seed=R.sim_key(seed, sigma), bond_ratio=R.WAD)
    hk = sim.v.hook
    seen = [0]
    orig = hk.snapshot

    def snap(*a, **k):
        out = orig(*a, **k)
        seen[0] = max(seen[0], hk.outBlocks)
        return out

    hk.snapshot = snap
    r = sim.run(B.make(name), I.DAYS * 24)
    return dict(seed=seed, kind=kind, sigma=sigma, bot=name, observer=obs, max_out=seen[0], breaches=r["breaches"],
                net=r["net"])


def job(args):
    seed, kind, sigma = args
    return [max_out_run(seed, kind, sigma, n, o) for n in CANDIDATES for o in (False, True)]


def l8_job(args):
    seed, kind, sigma = args
    S.SETTLE["rule"] = R.RULE
    S.SETTLE["int_closes"] = R.INT_CLOSES
    path = S.market_path(seed, sigma, I.DAYS, kind, edge=T0 + 1000)
    out = []
    for name in CANDIDATES:
        sim = S.Sim(path, 0.01, observer=False, sigma=sigma, rng_seed=R.sim_key(seed, sigma) + 1)   # L8, as regate_l3
        r = sim.run(B.make(name), I.DAYS * 24)
        out.append(dict(row="L8", bot=name, seed=seed, kind=kind, sigma=sigma, actions=r["actions"],
                        reverts=sum(r["revert_kinds"].values()), fallbacks=r["fallbacks"], breaches=r["breaches"],
                        bond_burn=r["bond_burn"]))
        sim = S.Sim(path, R.HONEST_M, observer=False, place=B.PLACES.get(name, True), sigma=sigma,
                    rng_seed=R.sim_key(seed, sigma), bond_ratio=R.WAD)                            # honest-cost row
        r = sim.run(B.make(name), I.DAYS * 24)
        out.append(dict(row="honest", bot=name, seed=seed, kind=kind, sigma=sigma, net=r["net"], charges=r["charges"],
                        gas=r["gas"], minted=r["minted"], actions=r["actions"], breaches=r["breaches"]))
    return out


def l8_summary(rows):
    out = {}
    for (kind, sigma) in I.KINDS:
        b = f"{kind}-{int(sigma * 100)}"
        for n in CANDIDATES:
            l8 = [r for r in rows if r["row"] == "L8" and r["bot"] == n and r["kind"] == kind and r["sigma"] == sigma]
            hc = [r for r in rows if r["row"] == "honest" and r["bot"] == n and r["kind"] == kind and r["sigma"] == sigma]
            acts = sum(r["actions"] for r in l8)
            out[f"{b}|{n}"] = dict(
                paths=len(l8), l8_blocked_share=(sum(r["reverts"] for r in l8) / acts if acts else 0.0),
                l8_fallbacks=sum(r["fallbacks"] for r in l8), l8_breached_paths=sum(1 for r in l8 if r["breaches"]),
                l8_bond_burn_mean=sum(r["bond_burn"] for r in l8) / len(l8),
                honest_net=sum(r["net"] for r in hc) / len(hc), honest_charges=sum(r["charges"] for r in hc) / len(hc),
                honest_gas=sum(r["gas"] for r in hc) / len(hc), honest_actions=sum(r["actions"] for r in hc) / len(hc),
                honest_breached_paths=sum(1 for r in hc if r["breaches"]))
    return out


def main_l8(procs, first, n):
    seeds = range(first, first + n)
    assert set(seeds) <= set(SPENT), "precheck uses spent seeds 100-699 only"
    jobs = [(sd, k, s) for (k, s) in I.KINDS for sd in seeds]
    t0 = time.time()
    with mp.Pool(procs) as pool:
        rows = [r for rs in pool.imap_unordered(l8_job, jobs, chunksize=4) for r in rs]
    summ = l8_summary(rows)
    ok = all(v["l8_breached_paths"] == 0 and v["honest_breached_paths"] == 0 and
             (v["l8_blocked_share"] <= 0.05 or not k.startswith("gbm-60|"))     # §8.1 L8: <= 5% at σ 60%, else reported
             for k, v in summ.items() if "|edge-12h" in k)
    os.makedirs(OUT, exist_ok=True)
    res = dict(seeds=[first, first + n - 1], summary=summ, ok_edge12h=ok, wall_s=round(time.time() - t0, 1))
    with open(os.path.join(OUT, f"precheck-l8-{first}-{n}.json"), "w") as fh:
        json.dump(dict(res, rows=rows), fh, allow_nan=False)
    print(f"L8 and honest-cost rows, seeds {first}-{first + n - 1}; {res['wall_s']} s")
    for k, v in summ.items():
        print(f"  {k:20s} L8 blocked {v['l8_blocked_share'] * 100:.3f}% fallbacks {v['l8_fallbacks']} breached "
              f"{v['l8_breached_paths']} bond burn {v['l8_bond_burn_mean']:.4f} | honest net {v['honest_net']:+.4f} "
              f"charges {v['honest_charges']:.4f} gas {v['honest_gas']:.4f} actions {v['honest_actions']:.1f} "
              f"breached {v['honest_breached_paths']}")
    print("edge-12h L8/honest (no breach; L8 blocked <= 5% at gbm-60):", "PASS" if ok else "FAIL")
    return 0 if ok else 1


def p99(xs):
    xs = sorted(xs)
    return xs[min(len(xs) - 1, int(0.99 * len(xs) + 0.999999) - 1)]   # nearest-rank


def summarise(rows, cure, grid):
    out = {}
    for (kind, sigma) in I.KINDS:
        b = f"{kind}-{int(sigma * 100)}"
        for n in CANDIDATES:
            for o in (False, True):
                rs = [r for r in rows if r["kind"] == kind and r["sigma"] == sigma and r["bot"] == n and r["observer"] == o]
                mo = [r["max_out"] for r in rs]
                q = p99(mo)
                out[f"{b}|{n}|{'obs' if o else 'noobs'}"] = dict(
                    n=len(rs), breached_paths=sum(1 for r in rs if r["breaches"] > 0), p99_max_out=q, max_max_out=max(mo),
                    margin=cure - q, ok=(sum(1 for r in rs if r["breaches"] > 0) == 0 and cure - q >= 3 * grid))
    return out


def main():
    a = [x for x in sys.argv[1:] if x != "--l8"]
    procs = int(a[0]) if len(a) > 0 else 4
    first = int(a[1]) if len(a) > 1 else 100
    n = int(a[2]) if len(a) > 2 else 600
    if "--l8" in sys.argv:
        return main_l8(procs, first, n)
    seeds = range(first, first + n)
    assert set(seeds) <= set(SPENT), "precheck uses spent seeds 100-699 only"
    t = S.Sim(S.market_path(first, 0.4, 1, "gbm", edge=T0 + 1000), M, observer=False).t
    cure, grid = t.CURE_BLOCKS, t.GRID
    jobs = [(sd, k, s) for (k, s) in I.KINDS for sd in seeds]
    t0 = time.time()
    with mp.Pool(procs) as pool:
        rows = [r for rs in pool.imap_unordered(job, jobs, chunksize=4) for r in rs]
    summ = summarise(rows, cure, grid)
    ok = all(v["ok"] for k, v in summ.items() if "|edge-12h|" in k)
    os.makedirs(OUT, exist_ok=True)
    res = dict(seeds=[first, first + n - 1], m=M, cure_blocks=cure, grid=grid, required_margin=3 * grid,
               ok_edge12h=ok, summary=summ, wall_s=round(time.time() - t0, 1))
    with open(os.path.join(OUT, f"precheck-{first}-{n}.json"), "w") as fh:
        json.dump(dict(res, rows=rows), fh, allow_nan=False)
    print(f"CURE {cure} blocks, GRID {grid}, required margin {3 * grid}; seeds {first}-{first + n - 1}; "
          f"{res['wall_s']} s")
    for k, v in summ.items():
        print(f"  {k:32s} n {v['n']:4d} breached {v['breached_paths']:3d} p99 {v['p99_max_out']:5d} "
              f"max {v['max_max_out']:5d} margin {v['margin']:5d} {'ok' if v['ok'] else 'FAIL'}")
    print("edge-12h D2:", "PASS" if ok else "FAIL")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
