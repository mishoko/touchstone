#!/usr/bin/env python3
"""Cell (a): incentive bots (G0 item 2) and the m proposal (spec method: m = max(m_IC, m_PC); the depositor ceiling
needs noise flow and is not measured here: the fee uplift and end NAV are reported), at the mainnet tier (12 s blocks, GRID 1 h, EPOCH 7 d, CURE 24 h, V 4 epochs) on 180-day synthetic
paths (driftless GBM at σ 40 / 60 / 120 %/yr = the p50 / p90 / p99 buckets; plus a hover path, an OU process
around the deploy range's upper edge at σ 60 %).

Phases:
- fit (seeds 0–99): every bot, both observer variants, at m = 0 and m = 1 %/yr. Net pay is affine in m
  (vested pay ∝ m; charges, gas and trading do not depend on m), so per path net(m) = m·P + C with
  P = (net(1%) − net(0)) / 1% and C = net(0). From these: the method's m_IC and m_PC, the fee uplift, and
  the smallest m at which every gated comparison passes on the fit seeds (m_gate).
- test (seeds 100–299): every bot, both variants, at the proposed m, plus the m = 0 control; the gate.

Net (ETH) = pay vested by the horizon (valued when it vests) − bond burned by charges − gas − the curator's own
spot-move trading loss. Pay still unvested at the horizon is reported separately (net_incl_unvested).
The mimic and each bot are paired in the same environment (`paired`): the fit and the gate use no observer
(§10); the observer environment is reported by the gate.
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
NAV = 1000.0                   # ETH; = BOND_MIN / BOND_RATIO, the NAV cap of a minimum-bond vault
ETH_USD = 3000.0               # tier A at T0
O_USD = 20_000.0               # operations, $/yr (spec default)
R_CARRY = 0.05                 # bond carry rate
BOND_ETH = 1.72                # BOND_MIN (owner, 2026-11-16)
SIGMAS = (0.40, 0.60, 1.20)
KINDS = [("gbm", s) for s in SIGMAS] + [("hover", 0.60)]
OUT = os.path.join(os.path.dirname(os.path.dirname(HERE)), "output", "g0")
YEARS = DAYS / 365.0
MIMIC = "edge60"               # the mimic of the gate; the repair re-gate (L3) pins edge-6h (set by the caller)
HOVER_CONTROL_REPORTED = False  # owner decision 2026-11-16: the m = 0 control is reported (not gated) on the hover path


def bucket(kind, sigma):
    return f"{kind}-{int(sigma * 100)}"


def paired(ix, b, s, bot, m, env=False, key="net"):
    """The ONE pairing rule for fit and gate (r1 R1-04): the mimic and the bot in the same environment; the fit
    and the gate use the no-observer environment (§10); lazy-under-ladder exists only with an observer."""
    if bot == "lazy-under-ladder":
        env = True
    return ix[(b, s, bot, m)][env][key], ix[(b, s, MIMIC, m)][env][key]


def run_job(args):
    seed, kind, sigma, ms, bots = args
    path = S.market_path(seed, sigma, DAYS, kind, edge=T0 + 1000)
    rows = []
    for name in bots:
        variants = (True,) if name == "lazy-under-ladder" else (False,) if name == "lazy" else (False, True)
        for obs in variants:
            for m in ms:
                sim = S.Sim(path, m, observer=obs, place=B.PLACES.get(name, True))
                r = sim.run(B.make(name), DAYS * 24)
                r.update(bot=name, seed=seed, kind=kind, sigma=sigma, m=m, observer=obs)
                rows.append(r)
    return rows


def run_all(seeds, ms, bots, procs=9):
    jobs = [(sd, k, s, ms, bots) for (k, s) in KINDS for sd in seeds]
    t0 = time.time()
    rows = []
    with mp.Pool(procs) as pool:
        for i, r in enumerate(pool.imap_unordered(run_job, jobs, chunksize=2)):
            rows.extend(r)
            if (i + 1) % 100 == 0:
                print(f"  {i + 1}/{len(jobs)} jobs, {time.time() - t0:.0f}s", flush=True)
    return rows


def index(rows):
    """(bucket, seed, bot, m) -> net per variant."""
    ix = {}
    for r in rows:
        ix.setdefault((bucket(r["kind"], r["sigma"]), r["seed"], r["bot"], r["m"]), {})[r["observer"]] = r
    return ix


# ----------------------------------------------------------------------------- fit: the m proposal
def fit(rows):
    ix = index(rows)
    seeds = sorted({r["seed"] for r in rows})
    out = {}
    for (kind, sigma) in KINDS:
        b = bucket(kind, sigma)
        e = [ix[(b, s, "edge60", 0.01)][False] for s in seeds]
        charges = sum(x["charges"] for x in e) / len(e) / YEARS
        gas = sum(x["gas"] - x["trade"] for x in e) / len(e) / YEARS
        f_m = sum(x["f"] for x in e) / len(e)
        fl = {}
        for bot in B.LAZY_FAMILY:
            vals = [paired(ix, b, s, bot, 0.01, key="f")[0] for s in seeds]
            fl[bot] = sum(vals) / len(vals)
        f_lazy = max(fl.values())
        q = pctl([c for x in e for c in x["action_charges"]], 0.99)
        m_ic = (charges + gas) / (NAV * (f_m - f_lazy)) if f_m > f_lazy else math.inf
        m_pc = (charges + gas + O_USD / ETH_USD + R_CARRY * BOND_ETH + q) / (NAV * f_m)
        # the fee uplift of edge60 over lazy (no observer), per NAV per year, arbitrage flow only. NOT a depositor
        # ceiling: at depth 0 the extra in-range time is LVR (end NAV is reported); the ceiling needs noise flow (Track B)
        lz = [ix[(b, s, "lazy", 0.01)][False] for s in seeds]
        fee_up = (sum(x["fees"] for x in e) - sum(x["fees"] for x in lz)) / len(e) / NAV / YEARS
        fa_up = sum(x["f_actual"] for x in e) / len(e) - sum(x["f_actual"] for x in lz) / len(lz)
        # the smallest m at which edge60 beats every gated bot on the fit seeds (paired CI lower bound > 0)
        m_need = {}
        m_need_obs = {}
        for bot in B.GATED:
            m_need[bot] = gate_min_m(ix, b, seeds, bot)
            if bot not in ("lazy",):   # lazy has no observer run (lazy-under-ladder is lazy with an observer)
                m_need_obs[bot] = gate_min_m(ix, b, seeds, bot, env=True)
        nav = {bot: sum(paired(ix, b, s, bot, 0.0, key="nav_end")[0] for s in seeds) / len(seeds)
               for bot in ("edge60", "lazy", "lazy-quiet", "breach-reset", "idle", "edge-6h")}
        # realised pay per unit of credited in-range time at m = 1% (pay ∝ NAV0, so LVR erodes the mimic's pay)
        ppt = {bot: sum(paired(ix, b, s, bot, 0.01, key="minted")[0] for s in seeds) /
               max(1e-12, sum(paired(ix, b, s, bot, 0.01, key="f")[0] for s in seeds)) for bot in ("edge60", "lazy-quiet")}
        m_need_max = max(m_need.values())
        out[b] = dict(charges_yr=charges, gas_yr=gas, f_mimic=f_m, f_lazy=fl, f_lazy_max=f_lazy, q=q, m_ic=m_ic,
                      m_pc=m_pc, fee_uplift=fee_up, f_actual_uplift=fa_up, m_need=m_need, m_need_max=m_need_max, m_need_observer=m_need_obs,
                      nav_end_m0=nav, pay_per_credited_f=ppt)
    return out


def gate_min_m(ix, b, seeds, bot, lo=0.0, hi=0.10, env=False):
    """Smallest m (bisection) with the paired one-sided 95% CI lower bound of (edge60 − bot) > 0 on the fit seeds,
    from the per-path affine fit net(m) = m·P + C, paired by `paired` (the same environment, no observer)."""
    def aff(s):
        b0, m0 = paired(ix, b, s, bot, 0.0, env)
        b1, m1 = paired(ix, b, s, bot, 0.01, env)
        return (m1 - m0) / 0.01, m0, (b1 - b0) / 0.01, b0

    A = [aff(s) for s in seeds]

    def lo_ci(m):
        return mean_ci([(pm * m + cm) - (pb * m + cb) for pm, cm, pb, cb in A])[1]

    if lo_ci(hi) <= 0:
        return math.inf
    if lo_ci(lo) > 0:
        return lo
    for _ in range(40):
        mid = (lo + hi) / 2
        if lo_ci(mid) > 0:
            hi = mid
        else:
            lo = mid
    return hi


# ----------------------------------------------------------------------------- test: the gate
ENVS = ((False, "no-observer"), (True, "observer"))   # no-observer is the pinned gating environment (spec §10)


def gate(rows, m):
    """Paired comparisons with the mimic and the bot in the SAME environment. The gate is the no-observer
    environment (pinned before the data); the observer environment (a third party calls refreshRef every 2·GRID)
    is reported."""
    ix = index(rows)
    seeds = sorted({r["seed"] for r in rows})
    res = {}
    for (kind, sigma) in KINDS:
        b = bucket(kind, sigma)
        res[b] = {}
        for env, ename in ENVS:
            mim = [paired(ix, b, s, "lazy" if env is False else "lazy-under-ladder", m, env)[1] for s in seeds]
            row = dict(mimic_mean=sum(mim) / len(mim), mimic_p5=pctl(mim, 0.05), mimic_p50=pctl(mim, 0.50), cmp={})
            for bot in B.GATED + tuple(x for x in B.REPORTED + ("edge60",) if x != MIMIC):
                if (b, seeds[0], bot, m) not in ix or env not in ix[(b, seeds[0], bot, m)] or \
                        (bot == "lazy-under-ladder") != env:
                    continue
                xs = [paired(ix, b, s, bot, m, env)[0] for s in seeds]   # r2 R2-06: the one pairing rule
                mean, lo, hi = mean_ci([a - x for a, x in zip(mim, xs)])
                row["cmp"][bot] = dict(bot_mean=sum(xs) / len(xs), diff=mean, ci_lo=lo, se=(hi - mean) / 1.645)
            lz0 = "lazy" if not env else "lazy-under-ladder"
            d0 = [a - c for a, c in (paired(ix, b, s, lz0, 0.0, env) for s in seeds)]
            row["control_m0"] = mean_ci(d0)
            e = [ix[(b, s, MIMIC, m)][env] for s in seeds]
            row["f_mimic"] = sum(x["f"] for x in e) / len(e)
            row["f_actual_mimic"] = sum(x["f_actual"] for x in e) / len(e)
            row["actions_yr"] = sum(x["actions"] for x in e) / len(e) / YEARS
            row["pings_yr"] = sum(x["pings"] for x in e) / len(e) / YEARS
            row["charges_yr"] = sum(x["charges"] for x in e) / len(e) / YEARS
            row["gas_yr"] = sum(x["gas"] for x in e) / len(e) / YEARS
            row["breached_mimic"] = sum(1 for x in e if x["breaches"] > 0)
            row["mimic_incl_unvested_mean"] = sum(x["net_incl_unvested"] for x in e) / len(e)
            res[b][ename] = row
    return res


def verdict(res, m):
    """The pre-registered rule on the gating environment; returns (ok, failures)."""
    fails = []
    for b, envs in res.items():
        g = envs["no-observer"]
        if b == "gbm-60" and g["mimic_p5"] < 0:
            fails.append(f"{b}: mimic p5 net {g['mimic_p5']:.4f} < 0")
        if g["mimic_mean"] < 0:
            fails.append(f"{b}: mimic mean net < 0")
        for bot in B.GATED:
            c = (envs["observer"] if bot == "lazy-under-ladder" else g)["cmp"].get(bot)
            if c is None:
                fails.append(f"{b}: no gating row for {bot}")   # r1 R1-08: a missing row fails
            elif c["ci_lo"] <= 0:
                fails.append(f"{b}: mimic > {bot} fails (diff {c['diff']:.4f}, CI lo {c['ci_lo']:.4f})")
        c = g["cmp"]["delayed-24h"]
        if c["diff"] < -2 * c["se"]:
            fails.append(f"{b}: delayed-24h > mimic + 2 SE")
        if g["control_m0"][1] <= 0 and not (HOVER_CONTROL_REPORTED and b.startswith("hover")):
            fails.append(f"{b}: control m = 0 (lazy ≥ mimic) CI lo {g['control_m0'][1]:.4f} <= 0")
    return not fails, fails


def main():
    os.makedirs(OUT, exist_ok=True)
    phase = sys.argv[1] if len(sys.argv) > 1 else "fit"
    if phase == "fit":
        n = int(sys.argv[2]) if len(sys.argv) > 2 else 100
        rows = run_all(range(0, n), (0.0, 0.01), B.ALL)
        with open(os.path.join(OUT, "bots-fit.json"), "w") as fh:
            json.dump(rows, fh)
        res = fit(rows)
        with open(os.path.join(OUT, "bots-fit-summary.json"), "w") as fh:
            json.dump(res, fh, indent=1, default=str)
        print(json.dumps(res, indent=1, default=str))
    elif phase == "refit":
        with open(os.path.join(OUT, "bots-fit.json")) as fh:
            res = fit(json.load(fh))
        with open(os.path.join(OUT, "bots-fit-summary.json"), "w") as fh:   # r2 R2-06: refit writes the summary
            json.dump(res, fh, indent=1, default=str)
        print(json.dumps(res, indent=1, default=str))
    elif phase in ("test", "regate"):
        ms = [float(x) for x in sys.argv[2].split(",")]
        n = int(sys.argv[3]) if len(sys.argv) > 3 else 200
        fn = os.path.join(OUT, "bots-test.json")
        if phase == "test":
            rows = run_all(range(100, 100 + n), tuple([0.0] + ms), B.ALL)
            with open(fn, "w") as fh:
                json.dump(rows, fh)
        else:
            with open(fn) as fh:
                rows = json.load(fh)
        out = {}
        for m in ms:
            res = gate(rows, m)
            ok, fails = verdict(res, m)
            out[str(m)] = dict(gate=res, ok=ok, fails=fails)
            print(f"m = {m * 100:.2f}%/yr: {'PASS' if ok else 'FAIL'} ({len(fails)} failing rows)")
            for f in fails:
                print("   ", f)
        with open(os.path.join(OUT, "bots-test-summary.json"), "w") as fh:
            json.dump(out, fh, indent=1, default=str)


if __name__ == "__main__":
    main()
