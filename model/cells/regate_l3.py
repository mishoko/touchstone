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

Usage: python3 model/cells/regate_l3.py <first_seed> <n_seeds> [procs]   (writes output/s5/l3-<first>-<n>.json/.txt)
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
OUT = os.path.join(ROOT, "output", "s5")
WAD = 10 ** 18


def job(args):
    seed, kind, sigma = args
    assert seed >= 100 or os.environ.get("TS_SMOKE"), "held-out seeds only (100-299 reported, 300-499 judged)"
    path = S.market_path(seed, sigma, I.DAYS, kind, edge=T0 + 1000)
    rows = []
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
              settles=r["settles"], breaches=r["breaches"], bond_burn=r["bond_burn"], extras_max=max(r["extras"], default=0.0))
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


if __name__ == "__main__":
    first, n = int(sys.argv[1]), int(sys.argv[2])
    procs = int(sys.argv[3]) if len(sys.argv) > 3 else 9
    jobs = [(sd, k, s) for (k, s) in I.KINDS for sd in range(first, first + n)]
    t0 = time.time()
    rows, l8 = [], []
    with mp.Pool(procs) as pool:
        for i, (r, x) in enumerate(pool.imap_unordered(job, jobs, chunksize=2)):
            rows.extend(r)
            l8.append(x)
            if (i + 1) % 50 == 0:
                print(f"{i + 1}/{len(jobs)} {time.time() - t0:.0f}s", flush=True, file=sys.stderr)
    os.makedirs(OUT, exist_ok=True)
    tag = f"{first}-{n}" + ("-smoke" if os.environ.get("TS_SMOKE") else "")
    with open(os.path.join(OUT, f"l3-{tag}.json"), "w") as fh:
        json.dump(dict(rows=rows, l8=l8), fh)
    out, txt = analyse(rows, l8)
    txt = f"L3/L8 seeds {first}-{first + n - 1} ({len(rows)} runs, {time.time() - t0:.0f} s)\n" + txt
    print(txt)
    with open(os.path.join(OUT, f"l3-{tag}.txt"), "w") as fh:
        fh.write(txt + "\n")
    with open(os.path.join(OUT, f"l3-{tag}-summary.json"), "w") as fh:
        json.dump(out, fh, indent=1, default=str)
