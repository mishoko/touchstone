#!/usr/bin/env python3
"""U-IC (spec §9.1; binding form in spec §10, "U-IC frozen form"): curator incentive compatibility in the simulation,
L3's registered rule on fresh seeds, run once and published whichever way it comes out.

Harness: the L3 re-gate's per-path job (`regate_l3.job`, unchanged): every bot of `bots.ALL` plus edge-6h, the
model's fee-aware markout settlement with integer closes, m ∈ {0, 1%, 2.2%}/yr, both observer environments, a bond
that never binds; one L8 run per path. Paths: `incentives.KINDS` (driftless GBM at σ 40 / 60 / 120 %/yr and the
hover path), 180 days.

Verdict (frozen; `uic_verdict`): `incentives.verdict` at m = 2.2%/yr with the mimic edge-6h and the hover m = 0
control reported (owner, #163): exactly L3's rule (`regate_l3.analyse`). In every bucket: mimic > each bot of
`bots.GATED` (the lazy family, dodge, breach-reset, idle, dust, stale, wide; paired one-sided 95% CI lower bound > 0;
lazy-under-ladder paired in the observer environment), mimic mean net ≥ 0, delayed-24h not above mimic + 2 SE; at
gbm-60 the mimic's p5 net ≥ 0; in every GBM bucket the m = 0 control (lazy − mimic CI lower bound > 0). The §9.1 row's
nine bots are a subset. A missing row fails.

Usage: python3 model/cells/uic.py [procs]   (seeds 500–699, fixed)
Guards: refuses a dirty `model/` or spec, a settlement other than the frozen one, and any second start
(`output/uic/STARTED` or the result exists). Writes eval/results/U-IC.json first (verdict, both environments' gate
rows, the whole L3-form summary: gate minimum m, honest cost, L8), then output/uic/ (the per-run rows, text).
"""
import json
import multiprocessing as mp
import os
import subprocess
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import bots as B  # noqa: E402
import incentives as I  # noqa: E402
import regate_l3 as R  # noqa: E402
import sim as S  # noqa: E402
from m2d_rate import model_hash  # noqa: E402

ROOT = R.ROOT
FIRST, N = 500, 200                      # fresh seeds 500–699 (seeds 0–499 were used by G0, L3 and F-046)
USED = range(0, 500)
M = 0.022
MIMIC = R.MIMIC                          # edge-6h
SECTION91 = B.LAZY_FAMILY + ("dodge", "breach-reset")   # the §9.1 row's bots (a subset of B.GATED)
RESULT = os.path.join(ROOT, "eval", "results", "U-IC.json")
OUT = os.path.join(ROOT, "output", "uic")
STARTED = os.path.join(OUT, "STARTED")
SPEC = os.path.join(ROOT, "internal", "TOUCHSTONE-SPEC.md")


def stream_keys(seeds):
    """Every RNG key a seed feeds, from the functions the run itself calls (`sim.path_key`, `regate_l3.sim_key`)."""
    keys = set()
    for sd in seeds:
        for (kind, sigma) in I.KINDS:
            keys.add(("path", S.path_key(sd, sigma, kind)))
            keys.add(("sim", R.sim_key(sd, sigma)))
            keys.add(("sim", R.sim_key(sd, sigma) + 1))
    return keys


def uic_verdict(rows, m=M):
    """L3's rule (`incentives.verdict`, mimic edge-6h, hover control reported) plus a per-bucket table."""
    I.MIMIC = MIMIC
    I.HOVER_CONTROL_REPORTED = True
    res = I.gate(rows, m)
    ok, fails = I.verdict(res, m)
    table = {}
    for b, envs in res.items():
        g = envs["no-observer"]
        t: dict = dict(mimic_mean=g["mimic_mean"], mimic_p5=g["mimic_p5"], cmp={})
        for bot in B.GATED:
            c = (envs["observer"] if bot == "lazy-under-ladder" else g)["cmp"].get(bot)
            if c is not None:
                t["cmp"][bot] = dict(diff=c["diff"], ci_lo=c["ci_lo"], bot_mean=c["bot_mean"], ok=c["ci_lo"] > 0,
                                     section_9_1=bot in SECTION91)
        dl = g["cmp"].get("delayed-24h")
        t["delayed_24h"] = dl and dict(diff=dl["diff"], se=dl["se"], ok=dl["diff"] >= -2 * dl["se"])
        c0 = g["control_m0"]
        t["control_m0"] = dict(diff_lazy_minus_mimic=c0[0], ci_lo=c0[1], ok=c0[1] > 0, gated=not b.startswith("hover"))
        table[b] = t
    return ok, fails, table, res


def nonvacuity(rows, m=M):
    out = {}
    for (kind, sigma) in I.KINDS:
        b = I.bucket(kind, sigma)
        xs = [r for r in rows if r["m"] == m and I.bucket(r["kind"], r["sigma"]) == b]
        d = {}
        for bot in (MIMIC,) + B.GATED:
            ys = [r for r in xs if r["bot"] == bot and (r["observer"] or bot != "lazy-under-ladder") and
                  (not r["observer"] or bot == "lazy-under-ladder")]
            d[bot] = dict(paths=len(ys), vested_pos=sum(1 for r in ys if r["vested"] > 0),
                          breached=sum(1 for r in ys if r["breaches"] > 0), actions=sum(r["actions"] for r in ys),
                          pokes=sum(r["pokes"] for r in ys), pings=sum(r["pings"] for r in ys))
        out[b] = d
    return out


def complete(rows, seeds):
    """Every (bucket, bot, m, environment) the harness makes has a row on every seed."""
    have = {}
    for r in rows:
        have.setdefault((I.bucket(r["kind"], r["sigma"]), r["bot"], r["m"], r["observer"]), set()).add(r["seed"])
    want = set(seeds)
    bad = []
    for (kind, sigma) in I.KINDS:
        for bot in R.BOTS:
            envs = (True,) if bot == "lazy-under-ladder" else (False,) if bot == "lazy" else (False, True)
            for obs in envs:
                for m in R.MS:
                    k = (I.bucket(kind, sigma), bot, m, obs)
                    if have.get(k) != want:
                        bad.append(k)
    return bad


def require_complete(rows, seeds):
    """An incomplete run is a harness defect, not evidence: it stops before the verdict and writes no result; it is
    recorded "not run" (spec §10, s23 design review S23D2-02)."""
    bad = complete(rows, seeds)
    if bad:
        sys.exit(f"incomplete: {len(bad)} (bucket, bot, m, observer) cells lack a seed (e.g. {bad[0]}); no result "
                 "written; record the run as not run")


def git(*a, cwd=ROOT):
    return subprocess.run(["git", "-C", cwd, *a], capture_output=True, text=True, check=True).stdout.strip()


def preflight():
    seeds = range(FIRST, FIRST + N)
    assert not (stream_keys(seeds) & stream_keys(USED)), "fresh seeds share an RNG stream with a used seed"
    assert R.RULE == "fee_aware" and R.INT_CLOSES, "U-IC runs the frozen settlement (fee-aware, integer closes)"
    for p in (RESULT, STARTED):
        if os.path.exists(p):
            sys.exit(f"{p} exists: U-IC runs once (spec §10); a restart needs the owner")
    if git("status", "--porcelain", "--", "model/"):
        sys.exit("model/ has uncommitted changes: U-IC runs at a committed model")
    internal = None
    if os.path.exists(SPEC):
        idir = os.path.dirname(SPEC)
        if git("status", "--porcelain", "--", "TOUCHSTONE-SPEC.md", cwd=idir):
            sys.exit("the spec has uncommitted changes: the frozen form must be committed before the run")
        internal = git("rev-parse", "HEAD", cwd=idir)
    meta: dict = dict(commit=git("rev-parse", "HEAD"), internal_commit=internal, model_hash=model_hash())
    return seeds, meta


def main():
    procs = int(sys.argv[1]) if len(sys.argv) > 1 else 4
    seeds, meta = preflight()
    os.makedirs(OUT, exist_ok=True)
    with open(STARTED, "w") as fh:
        json.dump(dict(meta, started=time.strftime("%Y-%m-%dT%H:%M:%S"), argv=sys.argv), fh)
    jobs = [(sd, k, s, False) for (k, s) in I.KINDS for sd in seeds]
    t0 = time.time()
    rows, l8 = [], []
    with mp.Pool(procs) as pool:
        for i, (r, x) in enumerate(pool.imap_unordered(R.job, jobs, chunksize=2)):
            rows.extend(r)
            l8.append(x)
            if (i + 1) % 20 == 0 or i + 1 == len(jobs):
                print(f"{i + 1}/{len(jobs)} {time.time() - t0:.0f}s", flush=True, file=sys.stderr)
    wall = time.time() - t0
    require_complete(rows, seeds)
    ok, fails, table, res = uic_verdict(rows)
    l3form, l3txt = R.analyse(rows, l8)
    meta.update(argv=sys.argv, procs=procs, wall_s=round(wall, 1), runs=len(rows), jobs=len(jobs),
                settlement=R.RULE, int_closes=R.INT_CLOSES)
    out = dict(
        eval="U-IC", verdict="PASS" if ok else "FAIL", fails=fails,
        frozen="spec §10 'U-IC frozen form' (s23, committed before the run: meta.internal_commit)",
        rule="incentives.verdict (L3's rule): mimic edge-6h, m 2.2%/yr, hover m = 0 control reported",
        seeds=[FIRST, FIRST + N - 1], m=M, mimic=MIMIC, gated_bots=list(B.GATED), section_9_1_bots=list(SECTION91),
        buckets=table, gate=res, l3_form=l3form, nonvacuity=nonvacuity(rows),
        scope="simulation: 180-day synthetic paths (driftless GBM, hover OU), third-party depth 0, NAV 1000 ETH, "
              "a bond that never binds; not the replay (E-IC)",
        meta=meta)
    if not ok:
        out.update(negative_result=True,
                   decision="spec §10 U-IC frozen form: published as failed; U-IC stays unmet; m stays 2.2%/yr; "
                            "never re-run on seeds 500-699; never superseded by a later row")
    with open(RESULT, "w") as fh:
        json.dump(out, fh, indent=1, default=str)
        fh.write("\n")
    tag = f"uic-{FIRST}-{N}"
    with open(os.path.join(OUT, tag + ".json"), "w") as fh:
        json.dump(dict(rows=rows, l8=l8), fh)
    txt = [f"U-IC seeds {FIRST}-{FIRST + N - 1}, mimic {MIMIC}, m {M * 100:g}%/yr: {out['verdict']} "
           f"({len(rows)} runs, {wall:.0f} s, commit {meta['commit']}, model {meta['model_hash']})"] + ["   " + f for f in fails]
    for b, t in table.items():
        txt.append(f"  {b}: mimic mean {t['mimic_mean']:+.4f} p5 {t['mimic_p5']:+.4f}; " +
                   "; ".join(f"{bot} {c['diff']:+.4f} (lo {c['ci_lo']:+.4f})" for bot, c in t["cmp"].items())
                   + f"; control m=0 lazy−mimic {t['control_m0']['diff_lazy_minus_mimic']:+.4f} "
                     f"(lo {t['control_m0']['ci_lo']:+.4f}){'' if t['control_m0']['gated'] else ' reported'}")
    txt = "\n".join(txt) + "\n\nL3 form (gate minimum m, honest cost, L8):\n" + l3txt
    print(txt)
    with open(os.path.join(OUT, tag + ".txt"), "w") as fh:
        fh.write(txt + "\n")
    return 0   # a completed run exits 0 whichever the verdict (the verdict is in RESULT); non-zero = a crash


def selfcheck():
    """The guards, without running a seed (s23 design review S23D-05/06/07): each must refuse its bad state."""
    import tempfile
    global RESULT, STARTED
    errs = []
    if stream_keys(range(FIRST, FIRST + N)) & stream_keys(USED):
        errs.append("fresh seeds overlap a used RNG key")
    if not stream_keys([7]) & stream_keys(range(0, 10)):
        errs.append("control: an overlapping seed set must share keys")
    saved = (R.RULE, RESULT, STARTED)
    try:
        R.RULE = "holdings_only"
        try:
            preflight()
            errs.append("a non-frozen settlement must be refused")
        except AssertionError as e:
            if "frozen settlement" not in str(e):
                errs.append(f"wrong refusal for the settlement: {e}")
        R.RULE = saved[0]
        with tempfile.TemporaryDirectory() as d:
            for name in ("RESULT", "STARTED"):
                RESULT, STARTED = os.path.join(d, "r.json"), os.path.join(d, "s")
                open(RESULT if name == "RESULT" else STARTED, "w").close()
                try:
                    preflight()
                    errs.append(f"an existing {name} must refuse a second start")
                except SystemExit as e:
                    if "runs once" not in str(e.code):
                        errs.append(f"wrong refusal for {name}: {e.code}")
                for q in (RESULT, STARTED):
                    if os.path.exists(q):
                        os.remove(q)
    finally:
        R.RULE, RESULT, STARTED = saved
    row = dict(kind="gbm", sigma=0.4, seed=FIRST, bot="lazy", m=0.0, observer=False)
    if not complete([row], [FIRST]):
        errs.append("control: one row cannot be complete")
    try:
        require_complete([row], [FIRST])
        errs.append("an incomplete run must stop before the verdict")
    except SystemExit as e:
        if "not run" not in str(e.code):
            errs.append(f"wrong stop for an incomplete run: {e.code}")
    for e in errs:
        print("uic selfcheck:", e)
    print("uic selfcheck", "OK" if not errs else "FAIL")
    return not errs


if __name__ == "__main__":
    if sys.argv[1:] == ["--selfcheck"]:
        sys.exit(0 if selfcheck() else 1)
    sys.exit(main())
