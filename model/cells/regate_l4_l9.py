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

Usage: python3 model/cells/regate_l4_l9.py FIRST:END [procs]   (writes output/s5/l4_l9_<FIRST>-<END-1>.txt)
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
from common import R, sqrt_at, sample_state, edge60_range, floor_sp, SPACING, W  # noqa: E402
from m2d import lags, grief_lane  # noqa: E402
from n4 import FEE  # noqa: E402

TOL = 10
FEE_TICKS = 5
RESERVE = 6e-4
N_ETA = 400
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
                        sq=sq, real_sq=(b["nav0"] - b["nav1"]) if sq is not None else None))
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


if __name__ == "__main__":
    first, end = (int(x) for x in sys.argv[1].split(":"))
    procs = int(sys.argv[2]) if len(sys.argv) > 2 else 9
    jobs = [(s, sg) for sg in (0.6, 1.2) for s in range(first, end)]
    with mp.Pool(procs) as p:
        r9 = [x for rr in p.map(l9, jobs) for x in rr]
        r4 = [x for rr in p.map(l4, jobs) for x in rr]
    s = f"L4/L9 seeds {first}–{end - 1}\n" + summarise(r9, r4)
    print(s)
    out = os.path.join(ROOT, "output", "s5")
    os.makedirs(out, exist_ok=True)
    with open(os.path.join(out, f"l4_l9_{first}-{end - 1}.txt"), "w") as fh:
        fh.write(s + "\n")
