#!/usr/bin/env python3
"""U-IC2 (spec §9.1; binding form in spec §10, "U-IC2 frozen form"; panel 2026-11-19-s24-fix-uic-e0 D1-D9): curator
incentive compatibility with the re-parameterised reference policy edge-12h (`EDGE_DELAY` 12 GRID, mainnet tier),
on fresh seeds 700-1099, run once and published whichever way it comes out. U-IC (edge-6h, seeds 500-699) stays
published and unmet; U-IC2 is its own row and never supersedes it.

Harness: `uic.py`'s, with exactly three differences (D3): the mimic (edge-12h), the seeds (700-1099, N = 400, in two
parts of 200) and the output paths. Each path runs `regate_l3.job` unchanged except that `regate_l3.MIMIC` is set to
edge-12h in every worker (the honest-cost row and L8 use the mimic); `regate_l3.BOTS` already holds edge-6h and
edge-12h (`bots.REPORTED`), at m in {0, 1%, 2.2%}/yr and both observer environments.

Verdict (frozen; `uic.uic_verdict` with the mimic edge-12h): `incentives.verdict` at m = 2.2%/yr, hover m = 0 control
reported; 52 comparisons, one-sided 95%, no multiplicity correction; a missing row fails. Plus the D4 lever control,
gated at gbm-120 only: the paired one-sided 95% CI lower bound of mean(edge-12h - edge-6h) > 0 (no observer, m 2.2%).
Reported, not gated: the whole verdict with the mimic edge-6h on the same seeds (D4: edge-6h - dodge and
edge-6h - breach-reset against U-IC's +0.086 / +0.012), and the observer environment for both mimics (D5).

Usage:
  python3 model/cells/uic2.py part 700 [procs]    # seeds 700-899
  python3 model/cells/uic2.py part 900 [procs]    # seeds 900-1099
  python3 model/cells/uic2.py verdict             # only after both parts; the one verdict, on all 400 seeds
  python3 model/cells/uic2.py --selfcheck
A part prints progress only, never a gate number (D3: no look after the first 200). Guards: a dirty `model/` or
spec, a settlement other than the frozen one, a second start of a part (`output/uic2/STARTED-<first>`), the result
already existing; the verdict refuses parts whose model hash or frozen-form hash differs from each other or from the
verdict's own (commits are recorded, not compared; S24U-08), and an incomplete set (a harness defect: the whole test is
recorded not run).
"""
import hashlib
import json
import math
import multiprocessing as mp
import os
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import bots as B  # noqa: E402
import incentives as I  # noqa: E402
import regate_l3 as R  # noqa: E402
import uic as U  # noqa: E402

ROOT = R.ROOT
PARTS = (700, 900)
PART_N = 200
FIRST, N = 700, 400                      # fresh seeds 700-1099 (0-699 spent: G0, L3, F-046, U-IC; fit data since s24)
USED = range(0, 700)
M = U.M
MIMIC = "edge-12h"
OLD = "edge-6h"                          # U-IC's mimic: the lever control's comparator and the reported form
LEVER_BUCKET = "gbm-120"
RESULT = os.path.join(ROOT, "eval", "results", "U-IC2.json")
OUT = os.path.join(ROOT, "output", "uic2")
SPEC = U.SPEC


def started(first):
    return os.path.join(OUT, f"STARTED-{first}")


def part_file(first):
    return os.path.join(OUT, f"part-{first}-{PART_N}.json")


def job(args):
    R.MIMIC = MIMIC                      # the honest-cost row and L8 use the mimic (spawned workers re-import)
    rows, l8 = R.job(args)
    if l8 is not None:
        l8["bot"] = R.MIMIC              # S24U-06: the data shows which policy L8 ran
    return rows, l8


def form_hash():
    """sha256 (16 hex) of the spec's "U-IC2 frozen form" block (S24U-08: the parts and the verdict must share the frozen
    form, not an internal commit, so an unrelated internal commit between the parts cannot void the run)."""
    if not os.path.exists(SPEC):
        return None
    t = open(SPEC).read()
    a = t.index("**U-IC2 frozen form")
    b = t.index("\n**Headline outputs:**", a)
    return hashlib.sha256(t[a:b].encode()).hexdigest()[:16]


def preflight_common():
    seeds = range(FIRST, FIRST + N)
    assert not (U.stream_keys(seeds) & U.stream_keys(USED)), "fresh seeds share an RNG stream with a used seed"
    assert R.RULE == "fee_aware" and R.INT_CLOSES, "U-IC2 runs the frozen settlement (fee-aware, integer closes)"
    assert MIMIC in R.BOTS and OLD in R.BOTS, "both mimics must be simulated on every path"
    if os.path.exists(RESULT):
        sys.exit(f"{RESULT} exists: U-IC2 runs once (spec §10); a restart needs the owner")
    if U.git("status", "--porcelain", "--", "model/"):
        sys.exit("model/ has uncommitted changes: U-IC2 runs at a committed model")
    internal = None
    if os.path.exists(SPEC):
        idir = os.path.dirname(SPEC)
        if U.git("status", "--porcelain", "--", "TOUCHSTONE-SPEC.md", cwd=idir):
            sys.exit("the spec has uncommitted changes: the frozen form must be committed before the run")
        internal = U.git("rev-parse", "HEAD", cwd=idir)
    meta: dict = dict(commit=U.git("rev-parse", "HEAD"), internal_commit=internal, model_hash=U.model_hash(),
                      form_hash=form_hash())
    return meta


def run_part(first, procs):
    if first not in PARTS:
        sys.exit(f"part must be one of {PARTS}")
    for p in (started(first), part_file(first)):
        if os.path.exists(p):
            sys.exit(f"{p} exists: each U-IC2 part runs once (spec §10); a restart needs the owner")
    meta = preflight_common()
    os.makedirs(OUT, exist_ok=True)
    with open(started(first), "w") as fh:
        json.dump(dict(meta, started=time.strftime("%Y-%m-%dT%H:%M:%S"), argv=sys.argv), fh)
    seeds = range(first, first + PART_N)
    jobs = [(sd, k, s, False) for (k, s) in I.KINDS for sd in seeds]
    t0 = time.time()
    rows, l8 = [], []
    with mp.Pool(procs) as pool:
        for i, (r, x) in enumerate(pool.imap_unordered(job, jobs, chunksize=2)):
            rows.extend(r)
            l8.append(x)
            if (i + 1) % 20 == 0 or i + 1 == len(jobs):
                print(f"{i + 1}/{len(jobs)} {time.time() - t0:.0f}s", flush=True, file=sys.stderr)
    meta.update(first=first, n=PART_N, procs=procs, wall_s=round(time.time() - t0, 1), runs=len(rows), jobs=len(jobs))
    with open(part_file(first), "w") as fh:
        json.dump(finite(dict(meta=meta, rows=rows, l8=l8)), fh, allow_nan=False)
    print(f"U-IC2 part {first}-{first + PART_N - 1}: {len(rows)} runs in {meta['wall_s']} s (no verdict until both "
          "parts exist)")
    return 0


def lever_control(res):
    """D4 (gated at gbm-120 only): with the mimic edge-12h, `incentives.gate`'s comparison against edge-6h is the
    paired mean(edge-12h - edge-6h) and its one-sided 95% CI lower bound, no observer, m 2.2%."""
    out = {}
    for b, envs in res.items():
        c = envs["no-observer"]["cmp"].get(OLD)
        out[b] = c and dict(diff=c["diff"], ci_lo=c["ci_lo"], gated=b == LEVER_BUCKET, ok=c["ci_lo"] > 0)
    return out


def observer_table(res):
    """D5 (reported): per bucket, the mimic minus each gated bot in the observer environment."""
    return {b: {bot: dict(diff=c["diff"], ci_lo=c["ci_lo"]) for bot, c in envs["observer"]["cmp"].items()
                if bot in B.GATED} for b, envs in res.items()}


def nav_end_table(rows, m=M):
    """D5/S24U-02 (reported): the vault's mean end NAV (ETH, 180 days, NAV 1000 at start; `sim` `nav_end`, the vault's
    whole holdings, curator bank and bond claims included) per bucket, environment and bot, at m 2.2%/yr, both mimics
    included; the zero-depth model (third-party depth 0)."""
    acc = {}
    for r in rows:
        if r["m"] != m:
            continue
        k = (I.bucket(r["kind"], r["sigma"]), "observer" if r["observer"] else "no-observer", r["bot"])
        acc.setdefault(k, []).append(r["nav_end"])
    out: dict = {}
    for (b, env, bot), xs in sorted(acc.items()):
        out.setdefault(b, {}).setdefault(env, {})[bot] = dict(mean=sum(xs) / len(xs), n=len(xs))
    return out


REPORTED_KEYS = ("observer_environment", "nav_end", "edge6h_form")   # spec §10 U-IC2 "Reported, not gated"


def finite(x):
    """Strict JSON (F-054): a non-finite float (`gate_min_m`'s inf: the last simulated m does not pass) is written as
    null."""
    if isinstance(x, float) and not math.isfinite(x):
        return None
    if isinstance(x, dict):
        return {k: finite(v) for k, v in x.items()}
    if isinstance(x, (list, tuple)):
        return [finite(v) for v in x]
    return x


FROZEN_KEYS = ("model_hash", "form_hash")


def check_frozen(metas, now):
    """S24U-01/08: both parts and the verdict's own code share the model hash and the frozen form's hash (no re-script
    after the data); commits are recorded, not compared."""
    for k in FROZEN_KEYS:
        vals = {m.get(k) for m in metas} | {now.get(k)}
        if len(vals) != 1:
            sys.exit(f"{k} differs between the parts and the verdict ({sorted(map(str, vals))}): not one frozen run; "
                     "record U-IC2 as not run")


def verdict_main():
    parts = []
    for first in PARTS:
        if not os.path.exists(part_file(first)):
            sys.exit(f"{part_file(first)} missing: the verdict needs both parts; a cut part means U-IC2 is not run")
        with open(part_file(first)) as fh:
            parts.append(json.load(fh))
    meta = preflight_common()
    check_frozen([p["meta"] for p in parts], meta)
    rows = [r for p in parts for r in p["rows"]]
    l8 = [x for p in parts for x in p["l8"]]
    seeds = range(FIRST, FIRST + N)
    U.require_complete(rows, seeds)
    U.MIMIC = MIMIC
    R.MIMIC = MIMIC
    ok_rule, fails, table, res = U.uic_verdict(rows)
    lever = lever_control(res)
    ok_lever = bool(lever.get(LEVER_BUCKET) and lever[LEVER_BUCKET]["ok"])
    if not ok_lever:
        c = lever.get(LEVER_BUCKET)
        fails = fails + [f"{LEVER_BUCKET}: lever control edge-12h > edge-6h fails" +
                         (f" (diff {c['diff']:.4f}, CI lo {c['ci_lo']:.4f})" if c else " (missing row)")]
    ok = ok_rule and ok_lever
    l3form, l3txt = R.analyse(rows, l8)
    obs12 = observer_table(res)
    # reported: the same rule with U-IC's mimic on the held-out seeds (D4) and its observer table (D5)
    U.MIMIC = OLD
    R.MIMIC = OLD
    ok6, fails6, table6, res6 = U.uic_verdict(rows)
    obs6 = observer_table(res6)
    U.MIMIC = MIMIC
    R.MIMIC = MIMIC
    I.MIMIC = MIMIC
    meta.update(parts=[p["meta"] for p in parts], runs=len(rows), settlement=R.RULE, int_closes=R.INT_CLOSES)
    out = dict(
        eval="U-IC2", verdict="PASS" if ok else "FAIL", fails=fails,
        frozen="spec §10 'U-IC2 frozen form' (s24, committed before the run: meta.parts[*].internal_commit and "
               "form_hash)",
        rule="incentives.verdict (L3's rule): mimic edge-12h, m 2.2%/yr, hover m = 0 control reported; plus the "
             "lever control edge-12h > edge-6h at gbm-120 (gated)",
        configuration="EDGE_DELAY = 12 GRID (mainnet tier); m 2.2%/yr; contract bytecode unchanged",
        selection="edge-12h is the only edge variant of the screened set (edge-3h, edge-6h, edge-12h, all simulated "
                  "since s2) that passes this rule on all three spent seed sets 100-299, 300-499 and 500-699; it was "
                  "named a candidate in F-025 at s2; it was selected after U-IC failed, and it was a reported column of "
                  "U-IC's run. Not screened: waits of 13-20 h (never simulated); delayed-24h (a 23 h wait) is excluded "
                  "because a 24-GRID EDGE_DELAY fails the deploy check (at most 6,250 blocks, about 20.8 h, on "
                  "mainnet). Panel 2026-11-19-s24-fix-uic-e0",
        seeds=[FIRST, FIRST + N - 1], m=M, mimic=MIMIC, gated_bots=list(B.GATED), section_9_1_bots=list(U.SECTION91),
        buckets=table, lever_control=lever, gate=res, l3_form=l3form, nonvacuity=U.nonvacuity(rows),
        reported=dict(
            observer_environment={MIMIC: obs12, OLD: obs6},
            nav_end=nav_end_table(rows),
            edge6h_form=dict(verdict="PASS" if ok6 else "FAIL", fails=fails6, buckets=table6,
                             uic_reference=dict(dodge=0.0862, breach_reset=0.0120))),
        scope="simulation: 180-day synthetic paths (driftless GBM, hover OU), third-party depth 0, NAV 1000 ETH, "
              "a bond that never binds, no third-party refresher of the reference in the gated environment; "
              "not the replay (E-IC)",
        history="U-IC (edge-6h, seeds 500-699) and L3 (edge-6h, seeds 300-499) failed and stay published; U-IC2 "
                "never supersedes them",
        null_means="an m_need of null: the last simulated m does not pass (gate_min_m returned inf; F-054: when only "
                   "the extrapolation beyond the last simulated m fails, the minimum is read on the simulated range; "
                   "U-IC.json was written before this change, at its pinned model hash)",
        meta=meta)
    # tautological by construction (the dict is built just above): it documents the §10 list and fails loudly if a
    # later edit drops an item from `reported` (S24U-02, S24U2-03)
    missing = [k for k in REPORTED_KEYS if k not in out["reported"]]
    assert not missing, f"a pre-registered reported item has no producer: {missing}"
    if not ok:
        out.update(negative_result=True,
                   decision="spec §10 U-IC2 frozen form: published as failed; no third attempt (panel D8); "
                            "EDGE_DELAY stays 12 GRID")
    with open(RESULT, "w") as fh:
        json.dump(finite(out), fh, indent=1, allow_nan=False)
        fh.write("\n")
    with open(os.path.join(OUT, f"uic2-{FIRST}-{N}.json"), "w") as fh:
        json.dump(finite(dict(rows=rows, l8=l8)), fh, allow_nan=False)
    txt = [f"U-IC2 seeds {FIRST}-{FIRST + N - 1}, mimic {MIMIC}, m {M * 100:g}%/yr: {out['verdict']} "
           f"({len(rows)} runs, model {meta['model_hash']})"] + ["   " + f for f in fails]
    for b, t in table.items():
        lv = lever.get(b)
        txt.append(f"  {b}: mimic mean {t['mimic_mean']:+.4f} p5 {t['mimic_p5']:+.4f}; " +
                   "; ".join(f"{bot} {c['diff']:+.4f} (lo {c['ci_lo']:+.4f})" for bot, c in t["cmp"].items()) +
                   (f"; lever edge-12h−edge-6h {lv['diff']:+.4f} (lo {lv['ci_lo']:+.4f})"
                    f"{' gated' if lv['gated'] else ' reported'}" if lv else ""))
    txt.append(f"reported: edge-6h form on these seeds: {'PASS' if ok6 else 'FAIL'}; " + "; ".join(fails6))
    txt = "\n".join(txt) + "\n\nL3 form (gate minimum m, honest cost, L8):\n" + l3txt
    print(txt)
    with open(os.path.join(OUT, f"uic2-{FIRST}-{N}.txt"), "w") as fh:
        fh.write(txt + "\n")
    return 0


def selfcheck():
    import tempfile
    global RESULT, OUT
    errs = []
    if U.stream_keys(range(FIRST, FIRST + N)) & U.stream_keys(USED):
        errs.append("fresh seeds overlap a used RNG key")
    if not U.stream_keys([650]) & U.stream_keys(USED):
        errs.append("control: a spent seed must share keys with USED")
    if set(range(PARTS[0], PARTS[0] + PART_N)) | set(range(PARTS[1], PARTS[1] + PART_N)) != set(range(FIRST, FIRST + N)):
        errs.append("the parts do not cover the seeds exactly")
    if MIMIC not in R.BOTS or OLD not in R.BOTS:
        errs.append("a mimic is not simulated")
    saved = (R.RULE, RESULT, OUT)
    try:
        R.RULE = "holdings_only"
        try:
            preflight_common()
            errs.append("a non-frozen settlement must be refused")
        except AssertionError as e:
            if "frozen settlement" not in str(e):
                errs.append(f"wrong refusal for the settlement: {e}")
        R.RULE = saved[0]
        with tempfile.TemporaryDirectory() as d:
            RESULT, OUT = os.path.join(d, "r.json"), d
            open(RESULT, "w").close()
            try:
                preflight_common()
                errs.append("an existing result must refuse a start")
            except SystemExit as e:
                if "runs once" not in str(e.code):
                    errs.append(f"wrong refusal for the result: {e.code}")
            os.remove(RESULT)
            # S24U-07: the guard tests point at spent seeds, so a regressed guard can never simulate a held-out seed,
            # and each must give its own refusal (checked before the tree's state, so a dirty tree cannot pass them)
            sp = (PARTS, PART_N)
            globals().update(PARTS=(650, 651), PART_N=1)
            try:
                try:
                    verdict_main()
                    errs.append("the verdict must refuse a missing part")
                except SystemExit as e:
                    if "missing" not in str(e.code) or "not run" not in str(e.code):
                        errs.append(f"wrong refusal for a missing part: {e.code}")
                open(started(650), "w").close()
                try:
                    run_part(650, 1)
                    errs.append("a started part must refuse a second start")
                except SystemExit as e:
                    if "each U-IC2 part runs once" not in str(e.code):
                        errs.append(f"wrong refusal for a second part start: {e.code}")
            finally:
                globals().update(PARTS=sp[0], PART_N=sp[1])
    finally:
        R.RULE, RESULT, OUT = saved
    # S24U-01/08: the verdict refuses parts whose model or frozen form differ from its own
    same = dict(model_hash="x", form_hash="f", commit="c1", internal_commit="i1")
    try:
        check_frozen([same, dict(same, commit="c2", internal_commit="i2")], same)
    except SystemExit:
        errs.append("commits alone must not void the run (S24U-08)")
    for k in FROZEN_KEYS:
        try:
            check_frozen([same, same], dict(same, **{k: "y"}))
            errs.append(f"a verdict at a different {k} must be refused")
        except SystemExit as e:
            if "not run" not in str(e.code):
                errs.append(f"wrong refusal for {k}: {e.code}")
    if form_hash() is None and os.path.exists(SPEC):
        errs.append("the frozen form's hash is missing")
    # S24U-04: gate_min_m on a non-monotone lower bound (fixture: mimic A, bot Z, grid 0 / 1% / 2.2%)
    saved_mimic = I.MIMIC
    try:
        I.MIMIC = "A"

        def ix_of(d):          # d: m -> mean(mimic - bot); per seed +/-0.001, so the CI follows the mean's sign
            ix = {}
            for sd in range(40):
                e = 0.001 if sd % 2 else -0.001
                for m, v in d.items():
                    ix[("b", sd, "Z", m)] = {False: dict(net=0.0)}
                    ix[("b", sd, "A", m)] = {False: dict(net=v + e)}
            return ix, range(40)
        ix, sds = ix_of({0.0: -0.1, 0.01: 0.1, 0.022: -0.1})
        if I.gate_min_m(ix, "b", sds, "Z", grid=(0.0, 0.01, 0.022)) != math.inf:
            errs.append("gate_min_m: a failing last simulated m must give inf, never a passing lower grid point")
        ix, sds = ix_of({0.0: -0.1, 0.01: 0.1, 0.022: 0.05})   # the extrapolation to 10% fails (F-054 branch)
        v = I.gate_min_m(ix, "b", sds, "Z", grid=(0.0, 0.01, 0.022))
        if not 0.0 < v < 0.01:
            errs.append(f"gate_min_m: the crossing must be bisected inside (0, 1%), not a grid point; got {v}")
    finally:
        I.MIMIC = saved_mimic
    fake = {"gbm-120": {"no-observer": {"cmp": {OLD: dict(diff=0.1, ci_lo=-0.01)}}},
            "gbm-40": {"no-observer": {"cmp": {OLD: dict(diff=-0.1, ci_lo=-0.2)}}}}
    lv = lever_control(fake)
    if lv["gbm-120"]["ok"] or not lv["gbm-120"]["gated"] or lv["gbm-40"]["gated"]:
        errs.append("lever control: gbm-120 only, CI lo > 0")
    for e in errs:
        print("uic2 selfcheck:", e)
    print("uic2 selfcheck", "OK" if not errs else "FAIL")
    return not errs


if __name__ == "__main__":
    a = sys.argv[1:]
    if a == ["--selfcheck"]:
        sys.exit(0 if selfcheck() else 1)
    if a[:1] == ["part"] and len(a) >= 2:
        sys.exit(run_part(int(a[1]), int(a[2]) if len(a) > 2 else 4))
    if a == ["verdict"]:
        sys.exit(verdict_main())
    sys.exit(__doc__)
