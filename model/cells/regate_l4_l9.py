#!/usr/bin/env python3
"""Re-gate rows L9 (honest charge) and L4 (grief) for the markout-settled charge, on held-out seeds.

Honest flow (the specification's reference policy): the curator's bundler checks that the pool's spot is within
TOL = 10 ticks of the reference pool's price u (the market within its arbitrage band, u = r + U(−5, 5)), re-aligns
spot to u inside its own transaction (s0 = u), and the vault removes its old position, moves spot to the edge target
when the in-place placement is not legal (empty after removal at third-party depth 0), and places. The immediate charge
is the worst of {s0, spot, reference}; the settlement adds min(reserve, max(0, loss at the worse of p_mk ± FEE_TICKS −
charge₀)), with p_mk = r + η and η the honest window error (the reference pool's close starts uniform in its band
and follows the market inside it over the ten 12-second blocks that start one block after the action).

L9: on every honest exit-state placement (lags paired with the exit direction), the charge in expectation over η is
≥ the depositors' realised loss (vault NAV at r before − after the arbitrage back to r). Control: the status-quo
charge {spot, reference} on the same state with spot first moved onto the lagging reference (F-027's rows).

L4: a non-colluding griefer pushes spot d ticks before the honest transaction and reverts after it. If the push takes
spot more than TOL from u the bundler reverts and the griefer only pays its round trip; otherwise the bundler
re-aligns. Griefer net = its P&L with the push − its P&L as a plain arbitrageur of the same placement (d = 0).
Control: the status-quo in-place placement without re-alignment or tolerance (`m2d.grief_lane`).

The fee-aware settlement (s16 rule, spec §3.4) is evaluated on the same L9 rows and η draws (`charges16`): s0 the
re-aligned spot, sEnd the spot the action leaves, the markout tick floor(r + η), the charge the total burn in shares
against a fixed S_act = n4.S_TOT, valued at the counterfactual NAV (the other holders are whole iff the burn B ≥
S_act·loss/nav0). L4's griefer net does not depend on the charge rule (the griefer holds no shares and the honest
transaction's outcome does not depend on the charge), so its rows are re-run unchanged.

Usage: python3 model/cells/regate_l4_l9.py FIRST:END [procs]   (writes output/$L9_OUT/l4_l9_<FIRST>-<END-1>.txt/.json,
default output/s16; L9_DIFF=1 adds the exact-tick C120 (v0) to every row)
"""
import math
import multiprocessing as mp
import os
import random
import sys

import numpy as np

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import m2d_rate as M  # noqa: E402
import json  # noqa: E402
from common import R, sqrt_at, sample_state, edge60_range, floor_sp, SPACING, W  # noqa: E402
from m2d import lags, grief_lane  # noqa: E402
from n4 import FEE  # noqa: E402

TOL = 10
FEE_TICKS = 5
RESERVE = 6e-4
N_ETA = 400
DIFF = os.environ.get("L9_DIFF") == "1"   # the exact-tick C120 (v0) on every row (the differential check)
OUT_TAG = os.environ.get("L9_OUT", "s16")
DS = (5, 10, 20, 40, 60)
ROOT = os.path.dirname(os.path.dirname(HERE))


def eta_honest(sigma, n, seed):
    """η = mean close over the window − the market at the action; the action block's close uniform in the band."""
    rng = np.random.default_rng(5_000_011 + seed * 17 + int(sigma * 100))
    T = M.WINDOW
    m = np.concatenate([np.zeros((n, 1)), np.cumsum(rng.standard_normal((n, T)) * M.sd12(sigma), axis=1)], axis=1)
    u = rng.uniform(-FEE_TICKS, FEE_TICKS, n)
    closes = []
    for k in range(1, T + 1):
        u = np.clip(u, m[:, k] - FEE_TICKS, m[:, k] + FEE_TICKS)
        closes.append(u.copy())
    return np.mean(closes, axis=0)


def honest(h, r, ref, u, d_signed=0, tol=TOL, realign=True):
    """The honest flow with an optional griefer push of d ticks first. Returns None if the bundler reverts or no
    legal placement exists, else dict(griefer P&L at r, before, after, s0, s_spot, nav0, nav1)."""
    sr = sqrt_at(r)
    pool = R.Pool(sr, FEE)
    if h.L:
        pool.modify("vault", h.lo, h.hi, h.L)
    ain, aout, zfo = pool.push_to(sqrt_at(r + d_signed)) if d_signed else (0, 0, False)
    g0, g1 = ((-ain, aout) if zfo else (aout, -ain)) if d_signed else (0, 0)
    if realign:
        if abs(pool.tick - u) > tol:
            return dict(reverted=True, pnl=_val(g0, g1, sr, pool, undo=True))
        pool.push_to(sqrt_at(u))                      # the bundler's re-alignment (its own trade)
    s0 = pool.s
    hb = h.copy()
    if hb.L:
        _, _, f0, f1 = pool.modify("vault", hb.lo, hb.hi, 0)
        hb.idle0 += f0
        hb.idle1 += f1
    before = hb.copy()
    nav0 = R.value(hb, sr)[0]
    if hb.L:
        a0, a1, _, _ = pool.modify("vault", hb.lo, hb.hi, -hb.L)
        hb = R.Holdings(hb.idle0 - a0, hb.idle1 - a1)
    inv = hb
    lo, _ = edge60_range(inv, ref, pool.tick)
    L, after, dl = R.placement_size(inv, lo, lo + W, pool.s, sqrt_at(ref))
    if R.placement_check(lo, lo + W, pool.tick, ref, 60, W, SPACING) or dl < R.WAD // 2 or L == 0:
        mostly0 = R.q(inv.idle0, sqrt_at(ref)) >= inv.idle1
        c = floor_sp(ref)
        lo, ps = (c, sqrt_at(c) + 1) if mostly0 else (c + SPACING - W, sqrt_at(c + SPACING) - 1)
        pool.push_to(ps)                              # the vault's own move after removal
        L, after, dl = R.placement_size(inv, lo, lo + W, pool.s, sqrt_at(ref))
        if R.placement_check(lo, lo + W, pool.tick, ref, 60, W, SPACING) or dl < R.WAD // 2 or L == 0:
            return None
    pool.modify("vault", lo, lo + W, L)
    s_spot = pool.s
    ain, aout, zfo = pool.push_to(sr)                 # the griefer's revert / the arbitrage back to r
    if d_signed:
        g0, g1 = (g0 - ain, g1 + aout) if zfo else (g0 + aout, g1 - ain)
    _, _, f0, f1 = pool.modify("vault", lo, lo + W, 0)
    hf = after.copy()
    hf.idle0 += f0
    hf.idle1 += f1
    nav1 = R.value(hf, sr)[0]
    return dict(reverted=False, pnl=_pnl(g0, g1, sr), before=before, after=after, s0=s0, s_spot=s_spot,
                tick_spot=R.get_tick_at_sqrt_price(s_spot), nav0=nav0, nav1=nav1)


def _pnl(g0, g1, sr):
    return R.q(g0, sr) + g1 if g0 >= 0 else g1 - R.mul_div_up(-g0, sr * sr, 1 << 192)


def _val(g0, g1, sr, pool, undo):
    """The griefer unwinds its push when the honest transaction reverts."""
    ain, aout, zfo = pool.push_to(sr)
    g0, g1 = (g0 - ain, g1 + aout) if zfo else (g0 + aout, g1 - ain)
    return _pnl(g0, g1, sr)


def charges(a, ref, r, etas):
    """(immediate charge₀ over {s0, spot, ref}, expected charge over η with the settlement extra capped at the
    reserve)."""
    legs = [a["s0"], a["s_spot"], sqrt_at(ref)]
    c0 = max(0, max(M.loss_at(a, p)[0] for p in legs))
    cap = RESERVE * R.value(a["before"], a["s_spot"])[0]
    xs = np.arange(math.floor(r + etas.min()) - 7, math.ceil(r + etas.max()) + 8, 0.25)
    ls = np.array([M.loss_at(a, M.sqrt_at_frac(float(x)))[0] for x in xs], dtype=float)
    mk = np.maximum(np.interp(r + etas - FEE_TICKS, xs, ls), np.interp(r + etas + FEE_TICKS, xs, ls))
    return c0, float(c0 + np.mean(np.minimum(np.maximum(0.0, mk - c0), cap)))


def charges16(a, ref, r, etas, c0):
    """The fee-aware settlement (spec §3.4, s16 rule) on an honest placement: s0 = the re-aligned spot, sEnd = the spot
    the action leaves, the markout tick floor(r + η) (an integer, as `tryWindow` returns it), the charge a share burn
    against S_act = n4.S_TOT (burn₀ at NAV' = the smallest NAV_after over {s0, spot, reference}). Returns the expected
    total burn valued at the counterfactual NAV (E[B]·nav0/S_act: the other holders are whole iff B ≥ S_act·loss/nav0),
    the expected settlement extra (`R.settle_extra`'s value of burnS), the share of draws with burnS > 0, the share on
    which the reserve caps burnS, the burn₀ value, and the ablation's expected charges (value rules: the charge; share
    rules: E[B]·nav0/S_act)."""
    legs = [a["s0"], a["s_spot"], sqrt_at(ref)]
    nav_burn = min(R.value(a["after"], p)[0] for p in legs)
    nav_imm = max(((M.loss_at(a, p)[0], M.loss_at(a, p)[1]) for p in legs), key=lambda x: x[0])[1]
    ctx = M.make_ctx(a["before"], a["after"], a["s0"], a["s_spot"], c0, nav_imm, nav_burn, R.value(a["before"], a["s_spot"])[0])
    pend = dict(s0=a["s0"], sEnd=a["s_spot"], charge0=c0, reserve=ctx["reserve"], burn0=ctx["burn0"], sAct=M.S_ACT)
    tk = np.floor(r + etas).astype(np.int64)
    uniq, inv = np.unique(tk, return_inverse=True)
    B, ex, pos, capb = [], [], [], []
    for t in uniq:
        extra, _, bs = R.settle_extra(a["before"], a["after"], pend, int(t), FEE_TICKS, FEE, ctx["reserve_shares"])
        B.append(ctx["burn0"] + bs)
        ex.append(extra)
        pos.append(bs > 0)
        capb.append(bs == ctx["reserve_shares"] and bs > 0)
    nav0 = a["nav0"]
    Bm = float(np.mean(np.array(B, dtype=float)[inv]))
    out = dict(c16=Bm * nav0 / M.S_ACT, x16=float(np.mean(np.array(ex, dtype=float)[inv])),
               pos16=float(np.mean(np.array(pos, dtype=float)[inv])), cap16=float(np.mean(np.array(capb, dtype=float)[inv])),
               b0v=ctx["burn0"] * nav0 / M.S_ACT)
    leg = M.legs_of(ctx)
    for name, cfg in M.VARIANTS:
        if name == "v0_s05x" and not DIFF:
            continue
        if cfg["int_tick"]:
            xs, iv = uniq, inv
        else:
            xs, iv = r + etas, np.arange(len(etas))
        vals = []
        for t in xs:
            burn, ch, _ = M.settle_var(ctx, leg, int(t) if cfg["int_tick"] else float(t), cfg)
            vals.append(ch if burn is None else burn * nav0 / M.S_ACT)
        out[name] = float(np.mean(np.array(vals, dtype=float)[iv]))
    assert abs(out["s16"] - out["c16"]) <= 1e-6 * max(1.0, abs(out["c16"])), "settle_var differs from R.settle_extra"
    return out


def l9(args):
    seed, sigma = args
    assert seed >= 100 or os.environ.get("TS_SMOKE"), "held-out seeds"
    rng = random.Random(seed * 31 + int(sigma * 100))
    etas = eta_honest(sigma, N_ETA, seed)
    h, r, _ = sample_state(seed, sigma, exit_state=True)
    sgn = 1 if r >= h.hi else -1
    out = []
    for ell in lags(seed, sigma, 12):
        ref = r - sgn * abs(ell)
        u = r + int(round(rng.uniform(-FEE_TICKS, FEE_TICKS)))
        a = honest(h, r, ref, u)
        if not a or a["reverted"]:
            continue
        c0, ce = charges(a, ref, r, etas)
        # control: the status-quo charge with spot first moved onto the lagging reference (the bundled move, F-027)
        b = honest(h, r, ref, ref, realign=True, tol=10 ** 6)
        sq = max(0, max(M.loss_at(b, p)[0] for p in (b["s_spot"], sqrt_at(ref)))) if b and not b["reverted"] else None
        out.append(dict(sigma=sigma, nav=a["nav0"], real=a["nav0"] - a["nav1"], c0=c0, ce=ce,
                        sq=sq, real_sq=(b["nav0"] - b["nav1"]) if sq is not None else None,
                        moved=a["s_spot"] != a["s0"], shift=R.get_tick_at_sqrt_price(a["s_spot"]) - R.get_tick_at_sqrt_price(a["s0"]),
                        **charges16(a, ref, r, etas, c0)))
    return out


def l4(args):
    seed, sigma = args
    assert seed >= 100 or os.environ.get("TS_SMOKE"), "held-out seeds"
    rng = random.Random(seed * 37 + int(sigma * 100))
    out = []
    for variant in ("exit", "in"):
        h, r, _ = sample_state(seed, sigma, exit_state=(variant == "exit"))
        nav = R.value(h, sqrt_at(r))[0]
        for d in DS:
            ctl = grief_lane(h, r, d)
            best = None
            for sg in (1, -1):
                u = r + int(round(rng.uniform(-FEE_TICKS, FEE_TICKS)))
                g = honest(h, r, r, u, sg * d)
                b = honest(h, r, r, u, 0)
                if g is None or b is None or b["reverted"]:
                    continue
                net = g["pnl"] - b["pnl"] if not g["reverted"] else g["pnl"]
                if best is None or net > best[0]:
                    best = (net, g["reverted"])
            out.append(dict(sigma=sigma, v=variant, d=d, nav=nav, ctl=ctl[0] if ctl else None,
                            g=best[0] if best else None, reverted=best[1] if best else None))
    return out


def summarise(r9, r4):
    bp = lambda x, n: 1e4 * x / n  # noqa: E731
    L = []
    for sg in (0.6, 1.2):
        g = [r for r in r9 if r["sigma"] == sg]
        short = [r for r in g if r["real"] > r["ce"] + 1]
        short0 = [r for r in g if r["real"] > r["c0"] + 1]
        over = sum(bp(r["ce"] - r["real"], r["nav"]) for r in g) / max(1, len(g))
        c = [r for r in g if r["sq"] is not None]
        cs = [r for r in c if r["real_sq"] > r["sq"] + 1]
        L.append(f"L9 σ {sg}: {len(g)} honest placements; expected charge < realised loss on {len(short)} "
                 f"(immediate legs alone: {len(short0)}); mean overcharge {over:.4f} bp/placement; "
                 f"max shortfall {max([bp(r['real'] - r['ce'], r['nav']) for r in short], default=0):.4f} bp | "
                 f"control (status quo, spot moved onto the reference first): {len(cs)} of {len(c)} short, "
                 f"max {max([bp(r['real_sq'] - r['sq'], r['nav']) for r in cs], default=0):.4f} bp")
    for sg in (0.6, 1.2):
        for v in ("exit", "in"):
            for d in DS:
                g = [r for r in r4 if r["sigma"] == sg and r["v"] == v and r["d"] == d]
                c = [r for r in g if r["ctl"] is not None]
                k = [r for r in g if r["g"] is not None]
                L.append(f"L4 σ {sg} {v:4s} d {d:2d}: griefer net max {max([bp(r['g'], r['nav']) for r in k], default=0):+.4f} bp, "
                         f"> 0 on {sum(r['g'] > 0 for r in k)}/{len(k)} (honest tx reverted by the tolerance on "
                         f"{sum(bool(r['reverted']) for r in k)}) | control (no re-alignment, no tolerance): max "
                         f"{max([bp(r['ctl'], r['nav']) for r in c], default=0):+.4f} bp, > 0 on {sum(r['ctl'] > 0 for r in c)}/{len(c)}")
    return "\n".join(L)


def summarise16(r9):
    """L9 under the s16 rule next to C120 on the same rows and η draws, the honest settlement cost and the ablation."""
    bp = lambda x, n: 1e4 * x / n  # noqa: E731
    L = ["L9 under the fee-aware settlement (s16 rule; the charge is the expected total burn valued at the "
         f"counterfactual NAV, E[B]·nav0/S_act, S_act = {M.S_ACT:.0e})"]
    for sg in (0.6, 1.2):
        g = [r for r in r9 if r["sigma"] == sg]
        if not g:
            continue
        sh = [r for r in g if r["real"] > r["c16"] + 1]
        sh0 = [r for r in g if r["real"] > r["b0v"] + 1]
        L.append(f"L9 s16 σ {sg}: {len(g)} honest placements; expected charge < realised loss on {len(sh)} (burn₀ alone: "
                 f"{len(sh0)}); max shortfall {max([bp(r['real'] - r['c16'], r['nav']) for r in sh], default=0):.2e} bp "
                 f"({max([r['real'] - r['c16'] for r in sh], default=0):.3g} wei); mean overcharge "
                 f"{sum(bp(r['c16'] - r['real'], r['nav']) for r in g) / len(g):.4f} bp/placement "
                 f"(C120: {sum(bp(r['ce'] - r['real'], r['nav']) for r in g) / len(g):.4f})")
        x16 = [r["x16"] / r["nav"] for r in g]
        x05 = [(r["ce"] - r["c0"]) / r["nav"] for r in g]
        L.append(f"  honest settlement extra per placement (fraction of NAV, expectation over η): s16 mean {np.mean(x16):.3e} "
                 f"max {max(x16):.3e} | C120 mean {np.mean(x05):.3e} max {max(x05):.3e}; s16 − C120 mean "
                 f"{np.mean(np.array(x16) - np.array(x05)):+.3e}, max {max(np.array(x16) - np.array(x05)):+.3e}")
        L.append(f"  burnS > 0 on {np.mean([r['pos16'] for r in g]) * 100:.1f}% of draws (placements with any: "
                 f"{sum(r['pos16'] > 0 for r in g)}/{len(g)}); the reserve caps burnS on {np.mean([r['cap16'] for r in g]) * 100:.3f}% of draws; "
                 f"rows with the vault's own move {sum(bool(r['moved']) for r in g)}")
        for name, _ in M.VARIANTS:
            if name not in g[0]:
                continue
            shv = sum(1 for r in g if r["real"] > r[name] + 1)
            L.append(f"  ablation {name:10s}: short on {shv}; mean overcharge {sum(bp(r[name] - r['real'], r['nav']) for r in g) / len(g):.4f} bp")
        if "v0_s05x" in g[0]:
            L.append(f"  differential v0 (exact ticks, every s16 element off) vs C120 (0.25-tick grid): max |Δcharge| "
                     f"{max(bp(abs(r['v0_s05x'] - r['ce']), r['nav']) for r in g):.2e} bp")
    return "\n".join(L)


if __name__ == "__main__":
    first, end = (int(x) for x in sys.argv[1].split(":"))
    procs = int(sys.argv[2]) if len(sys.argv) > 2 else 9
    jobs = [(s, sg) for sg in (0.6, 1.2) for s in range(first, end)]
    with mp.Pool(procs) as p:
        r9 = [x for rr in p.map(l9, jobs) for x in rr]
        r4 = [x for rr in p.map(l4, jobs) for x in rr]
    s = f"L4/L9 seeds {first}–{end - 1}\n" + summarise(r9, r4) + "\n" + summarise16(r9)
    print(s)
    out = os.path.join(ROOT, "output", OUT_TAG)
    os.makedirs(out, exist_ok=True)
    with open(os.path.join(out, f"l4_l9_{first}-{end - 1}.txt"), "w") as fh:
        fh.write(s + "\n")
    with open(os.path.join(out, f"l4_l9_{first}-{end - 1}.json"), "w") as fh:
        json.dump(dict(r9=r9, r4=r4), fh, default=str)
