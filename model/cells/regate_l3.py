#!/usr/bin/env python3
"""Item 2 under the markout-settled charge (re-gate row L3) and the liveness row (L8), on held-out seeds.

L3: every bot of the incentives gate is simulated with the specification's charge (the model's markout settlement,
`sim.py`), edge-6h as the mimic, at m ∈ {0, 1%, 2.2%}/yr, both observer environments. The verdict is the registered
`incentives.verdict` with the mimic set to edge-6h and the m = 0 control reported (not gated) on the hover path.
The row passes iff the verdict passes at m = 2.2%/yr (every no-observer row at m ≤ 2.2%/yr; the comparisons improve
with m). The gate minimum m per bucket (the smallest m with every gated comparison's CI lower bound > 0) is reported.
The honest curator tops up its bond (modelled as a bond large enough never to bind; bond burns are costs either way).

L8: edge-6h at m = 1%/yr with the bond at `BOND_RATIO`·S from seed, eroding with every bond burn and never topped up,
the solvency gate enforced: the share of wanted actions that revert (by error), settlement fallbacks and breaches.

The settlement applied is the model's (fee-aware, spec §3.4) unless L3_RULE=holdings_only selects the holdings-only
rule (`sim.SETTLE`); every settlement also records the other rules on the same pending record, which gives the honest
settlement cost of the mimic per bucket. The markout read floors each block's close to an integer tick before the
window mean is floored (`sim.SETTLE["int_closes"]`; L3_CONT_CLOSES=1 restores the continuous mean of earlier runs).

HONEST-COST ROW (S16F-02): the mimic (edge-6h) at m = 2.2%/yr without an observer, every settlement classed by the
action it settles (all; placements; placements with / without the vault's own move; exit re-entries (the market
outside the range before the action) / in-range re-placements; swaps): per class the count, mean and max burnS
(fraction of the supply S_act) and extra (fraction of NAV), the share with burnS > 0, the reserve-capped count and the
annual cost in %NAV/yr (mean over paths, 90% CI), under the fee-aware rule next to the holdings-only rule.

Usage: python3 model/cells/regate_l3.py <first_seed> <n_seeds> [procs] [--honest-only] [--out DIR]
--honest-only runs only the honest-cost row's simulations (the mimic, m = 2.2%/yr, no observer; no L3 verdict, no L8).
Writes DIR/l3-<rule>-<first>-<n>[-honest].json/.txt (DIR: --out, else $L3_OUT_DIR, else output/fee_aware); the summary
JSON's `meta` holds the commit, the command and the wall time. Seeds < 100 need TS_SMOKE=1.
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
import incentives as I  # noqa: E402
from common import T0, mean_ci  # noqa: E402

MS = (0.0, 0.01, 0.022)
MIMIC = "edge-6h"
BOTS = tuple(dict.fromkeys((MIMIC, "edge60") + B.GATED + B.REPORTED))
ROOT = os.path.dirname(os.path.dirname(HERE))
OUT = os.environ.get("L3_OUT_DIR", os.path.join(ROOT, "output", "fee_aware"))
RULE = os.environ.get("L3_RULE", "fee_aware")    # the settlement applied: fee_aware (the model's) or holdings_only
assert RULE in S.RULES, RULE
INT_CLOSES = os.environ.get("L3_CONT_CLOSES") != "1"
S.SETTLE["rule"] = RULE
S.SETTLE["int_closes"] = INT_CLOSES
WAD = 10 ** 18
HONEST_M = 0.022


def job(args):
    seed, kind, sigma, honest_only = args
    S.SETTLE["rule"] = RULE
    S.SETTLE["int_closes"] = INT_CLOSES
    assert seed >= 100 or os.environ.get("TS_SMOKE"), "held-out seeds only (100-299 reported, 300-499 judged)"
    path = S.market_path(seed, sigma, I.DAYS, kind, edge=T0 + 1000)
    rows = []
    if honest_only:   # the honest-cost row only: the mimic at m = 2.2%/yr, no observer (the same Sim as the full run's)
        sim = S.Sim(path, HONEST_M, observer=False, place=B.PLACES.get(MIMIC, True), sigma=sigma,
                    rng_seed=seed * 1009 + int(sigma * 100), bond_ratio=WAD)
        r = sim.run(B.make(MIMIC), I.DAYS * 24)
        r.pop("action_charges")
        r.update(bot=MIMIC, seed=seed, kind=kind, sigma=sigma, m=HONEST_M, observer=False)
        return [r], None
    for name in BOTS:
        variants = (True,) if name == "lazy-under-ladder" else (False,) if name == "lazy" else (False, True)
        for obs in variants:
            for m in MS:
                sim = S.Sim(path, m, observer=obs, place=B.PLACES.get(name, True), sigma=sigma,
                            rng_seed=seed * 1009 + int(sigma * 100), bond_ratio=WAD)
                r = sim.run(B.make(name), I.DAYS * 24)
                r.pop("action_charges")
                r.update(bot=name, seed=seed, kind=kind, sigma=sigma, m=m, observer=obs)
                rows.append(r)
    # L8: the mimic with an eroding BOND_RATIO·S bond and the gate enforced
    sim = S.Sim(path, 0.01, observer=False, sigma=sigma, rng_seed=seed * 1009 + int(sigma * 100) + 1)
    r = sim.run(B.make(MIMIC), I.DAYS * 24)
    l8 = dict(seed=seed, kind=kind, sigma=sigma, actions=r["actions"], reverts=r["revert_kinds"], fallbacks=r["fallbacks"],
              settles=r["settles"], breaches=r["breaches"], bond_burn=r["bond_burn"], extras_max=max(r["extras"], default=0.0),
              settle_stats=r["settle_stats"])
    return rows, l8


def analyse(rows, l8):
    I.MIMIC = MIMIC
    I.HOVER_CONTROL_REPORTED = True
    lines = []
    out = {}
    res = I.gate(rows, 0.022)
    ok, fails = I.verdict(res, 0.022)
    out["verdict_m2.2"] = dict(ok=ok, fails=fails)
    lines.append(f"L3 verdict at m = 2.2%/yr (mimic {MIMIC}; hover control reported): {'PASS' if ok else 'FAIL'}")
    lines += ["   " + f for f in fails]
    ix = I.index(rows)
    seeds = sorted({r["seed"] for r in rows})
    for (kind, sigma) in I.KINDS:
        b = I.bucket(kind, sigma)
        need = {bot: I.gate_min_m(ix, b, seeds, bot) for bot in B.GATED}
        g = res[b]["no-observer"]
        c0 = g["control_m0"]
        dl = g["cmp"]["delayed-24h"]
        out[b] = dict(m_need=need, m_need_max=max(need.values()), control_m0=c0, mimic_p5=g["mimic_p5"],
                      mimic_mean=g["mimic_mean"], charges_yr=g["charges_yr"], actions_yr=g["actions_yr"],
                      delayed=dict(diff=dl["diff"], se=dl["se"]),
                      observer_m_need=max(I.gate_min_m(ix, b, seeds, bot, env=True) for bot in B.GATED if bot != "lazy"))
        worst = max(need, key=need.get)
        lines.append(f"  {b}: gate minimum m {max(need.values()) * 100:.2f}%/yr (worst {worst}); observer env (reported) "
                     f"{out[b]['observer_m_need'] * 100:.2f}%; control m=0 diff {c0[0]:+.4f} CI lo {c0[1]:+.4f}"
                     f"{' (reported)' if kind == 'hover' else ''}; mimic mean {g['mimic_mean']:+.3f} p5 {g['mimic_p5']:+.3f} ETH; "
                     f"charges {g['charges_yr']:.2f} ETH/yr, actions {g['actions_yr']:.0f}/yr; delayed-24h − mimic "
                     f"{-dl['diff']:+.3f} (SE {dl['se']:.3f})")
    hc, hl = honest_cost(rows)
    out["honest_cost"] = hc
    lines += hl
    # L8
    lines.append("L8 liveness (edge-6h, m = 1%, eroding BOND_RATIO·S bond, gate enforced, no top-up):")
    for (kind, sigma) in I.KINDS:
        xs = [x for x in l8 if x["kind"] == kind and x["sigma"] == sigma]
        acts = sum(x["actions"] for x in xs)
        rev = {}
        for x in xs:
            for k, v in x["reverts"].items():
                rev[k] = rev.get(k, 0) + v
        blocked = sum(v for k, v in rev.items() if k != "NoLegalPlacement")
        share = blocked / max(1, acts + blocked)
        out.setdefault("l8", {})[I.bucket(kind, sigma)] = dict(actions=acts, reverts=rev, blocked_share=share,
                                                              fallbacks=sum(x["fallbacks"] for x in xs),
                                                              breached=sum(1 for x in xs if x["breaches"] > 0))
        lines.append(f"  {I.bucket(kind, sigma)}: blocked {share * 100:.3f}% of {acts + blocked} wanted actions; reverts {rev}; "
                     f"fallback settlements {out['l8'][I.bucket(kind, sigma)]['fallbacks']}; paths breached "
                     f"{out['l8'][I.bucket(kind, sigma)]['breached']}/{len(xs)}; max honest extra "
                     f"{max(x['extras_max'] for x in xs):.2e} NAV")
    return out, "\n".join(lines)


def honest_cost(rows):
    """The honest-cost row: the mimic (edge-6h, no observer) at m = 2.2%/yr; every settlement evaluated under the
    fee-aware rule, the holdings-only rule and the holdings-only rule with the upper leg rounded up, on the same pending
    records, classed by the settled action (`sim.Sim.SETTLE_CLASSES`)."""
    lines = [f"Honest settlement cost (edge-6h, m = 2.2%/yr, no observer; applied rule {RULE}; markout closes "
             f"{'integer per block' if INT_CLOSES else 'continuous'}; the other rules computed on the same pending records). "
             "burnS as a fraction of S_act, extra as a fraction of NAV; per class: settlements, burnS mean / max, share "
             "with burnS > 0, extra mean / max, %NAV/yr [90% CI] (fee-aware) | holdings-only %NAV/yr:"]
    out = {}
    yr = 365 / I.DAYS
    for (kind, sigma) in I.KINDS:
        xs = [r for r in rows if r["bot"] == MIMIC and not r["observer"] and r["m"] == HONEST_M and r["kind"] == kind
              and r["sigma"] == sigma]
        if not xs:
            continue
        b = I.bucket(kind, sigma)
        out[b] = {}
        lines.append(f"  {b} ({len(xs)} paths):")
        for cls in S.Sim.SETTLE_CLASSES:
            st = [r["settle_stats"][cls] for r in xs]
            n = sum(x["n"] for x in st)
            d = dict(n=n, per_path_yr=n / len(st) * yr, cap_fa=sum(x["cap_fa"] for x in st))
            for k in S.Sim.SETTLE_KEYS:
                d[k + "_mean"] = sum(x[k + "_sum"] for x in st) / max(1, n)
                d[k + "_max"] = max(x[k + "_max"] for x in st)
                d[k + "_pos_share"] = sum(x[k + "_pos"] for x in st) / max(1, n)
                d[k + "_yr"] = mean_ci([x[k + "_sum"] * yr for x in st])
            out[b][cls] = d
            if n == 0:
                lines.append(f"    {cls:12s}: 0 settlements")
                continue
            lines.append(f"    {cls:12s}: {n} ({d['per_path_yr']:.0f}/yr); burnS mean {d['b_fa_mean']:.3e} max {d['b_fa_max']:.3e}, "
                         f"> 0 on {d['b_fa_pos_share'] * 100:.1f}%; extra mean {d['x_fa_mean']:.3e} max {d['x_fa_max']:.3e}; "
                         f"{d['x_fa_yr'][0] * 100:.4f} [{d['x_fa_yr'][1] * 100:.4f}, {d['x_fa_yr'][2] * 100:.4f}] %NAV/yr | "
                         f"holdings-only {d['x_ho_yr'][0] * 100:.4f} (upper leg + 1: {d['x_ho1_yr'][0] * 100:.4f}); "
                         f"fee-aware − holdings-only {(d['x_fa_yr'][0] - d['x_ho_yr'][0]) * 100:+.4f} %NAV/yr; reserve caps "
                         f"{d['cap_fa']}")
    return out, lines


if __name__ == "__main__":
    import subprocess
    args = sys.argv[1:]
    if "--out" in args:
        j = args.index("--out")
        OUT = os.path.abspath(args[j + 1])
        args = args[:j] + args[j + 2:]
    honest_only = "--honest-only" in args
    args = [x for x in args if x != "--honest-only"]
    first, n = int(args[0]), int(args[1])
    procs = int(args[2]) if len(args) > 2 else 2
    jobs = [(sd, k, s, honest_only) for (k, s) in I.KINDS for sd in range(first, first + n)]
    t0 = time.time()
    rows, l8 = [], []
    with mp.Pool(procs) as pool:
        for i, (r, x) in enumerate(pool.imap_unordered(job, jobs, chunksize=1 if honest_only else 2)):
            rows.extend(r)
            if x is not None:
                l8.append(x)
            if (i + 1) % 10 == 0 or i + 1 == len(jobs):
                print(f"{i + 1}/{len(jobs)} {time.time() - t0:.0f}s", flush=True, file=sys.stderr)
    wall = time.time() - t0
    try:
        commit = subprocess.run(["git", "-C", ROOT, "rev-parse", "HEAD"], capture_output=True, text=True, check=True).stdout.strip()
        dirty = bool(subprocess.run(["git", "-C", ROOT, "status", "--porcelain", "--untracked-files=no"], capture_output=True,
                                    text=True, check=True).stdout.strip())
    except (OSError, subprocess.CalledProcessError):
        commit, dirty = None, None
    os.makedirs(OUT, exist_ok=True)
    tag = f"{RULE}-{first}-{n}" + ("-honest" if honest_only else "") + ("-smoke" if os.environ.get("TS_SMOKE") else "")
    with open(os.path.join(OUT, f"l3-{tag}.json"), "w") as fh:
        json.dump(dict(rows=rows, l8=l8), fh)
    if honest_only:
        out, lines = honest_cost(rows)
        txt = "\n".join(lines)
    else:
        out, txt = analyse(rows, l8)
    out["meta"] = dict(commit=commit, dirty=dirty, argv=sys.argv, wall_s=round(wall, 1), runs=len(rows), rule=RULE,
                       int_closes=INT_CLOSES)
    txt = (f"L3{'' if honest_only else '/L8'} seeds {first}-{first + n - 1}, settlement {RULE} ({len(rows)} runs, {wall:.0f} s; "
           f"commit {commit}{' (uncommitted changes)' if dirty else ''})\n" + txt)
    print(txt)
    with open(os.path.join(OUT, f"l3-{tag}.txt"), "w") as fh:
        fh.write(txt + "\n")
    with open(os.path.join(OUT, f"l3-{tag}-summary.json"), "w") as fh:
        json.dump(out, fh, indent=1, default=str)
