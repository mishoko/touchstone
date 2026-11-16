#!/usr/bin/env python3
"""M-2d rate lane, corrected: the push free to the fence, waits up to the cure window, deep-spot offsets with tails
and lags paired with the path.

The attack is the self-sandwich of cell (f): a coalition (the curator plus a Sybil with ordering control) pushes the
pool price d ticks from the market price r, the curator re-places the vault at the pushed spot (any legal W-wide
range containing spot and the reference, plus the two single-sided edge placements), and the Sybil swaps the price
back to r. Everything is valued at r; the curator holds the NAV-cap fraction of the supply and pays the charge by a
share burn (cell (d)'s accounting, `n4.coalition_net`).

Compared with the cell (f) rate lane:
- the push is free up to the fence: d ∈ {5, 10, 15, 20, 30, …, 120}, both directions, third-party depth k ∈ {0, 1};
- the reference lag is paired with the state: in an exit state the 10-minute TWAP trails the move that took the
  market out of the vault's range (the lag points back toward the old range); in range its sign is random; its
  magnitude is the TWAP lag of a 1-minute GBM path (`m2d.lags`);
- two charge rules are evaluated on the same physical attack (the attack's outcome does not depend on the charge,
  only its price does):
    sq   the current rule: the worse of {spot, reference} (the positive control: its rate must be > 0);
    C60  the candidate repair: the worse of {pre-move spot, spot, reference, markout}, where the pre-move spot is the
         spot read before the vault's own move (the colluder may pre-set it anywhere on its push path; the lane
         takes the worst case for the defence, the pushed spot itself) and the markout price is the deep pool's
         TWAP over the 60 s after the action: market + η, η ~ Normal(0, sd12²·5/3 + 25/15) (the market's moves over
         five 12-second blocks plus the deep pool's ±5-tick arbitrage band averaged over five blocks), clamped to
         spot ± 120 ticks. The stress replaces the Normal by a Student-t with 3 degrees of freedom at the same
         standard deviation.
  For C60 the colluder commits before the markout is known, so an attack's net is its expectation over η
  (deterministic quadrature: 32 Gauss–Hermite nodes for the Normal, 64 equal-probability quantiles for the t3);
  the luckiest draw is reported for information only.
- a cycle's net is the best net over all legal attacks (0 if the coalition abstains); the rate per year is the
  opportunity count (`m2d.n_opps`: once per COOLDOWN in range, and per exit the best of 10 COOLDOWN slots) times
  E[max(0, cycle net)], summed over the exit and in-range lanes. A C60 cycle with no attack of positive expected
  net contributes 0 whatever the wait, because the markout does not depend on the state at the action.

Bound: ≤ 0.1% of NAV per year at σ 60% and ≤ 0.6% at σ 120%; the Student-t3 stress ≤ 2× the bound.
Fit seeds only (0–99). Usage: python3 model/cells/m2d_rate.py [n_seeds] [procs]
"""
import json
import math
import multiprocessing as mp
import os
import random
import sys
import time

import numpy as np

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from common import R, sqrt_at, W, SPACING, floor_sp, sample_state, mean_ci  # noqa: E402
from n4 import coalition_net, candidates, F_CAP, FEE  # noqa: E402
from m2d import lags, third_range, n_opps  # noqa: E402

DS = (5, 10, 15, 20, 30, 40, 50, 60, 70, 80, 90, 100, 110, 120)
KS = (0, 1)
BAND = 60
CLAMP = 120
H_BLOCKS = 5
BOUND = {0.6: 1e-3, 1.2: 6e-3}
LAGS_PER_STATE = 3
ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
OUT = os.path.join(ROOT, "output", "s4")


def sd12(sigma):
    return sigma * math.sqrt(12 / (365 * 86400)) / math.log(1.0001)


def eta_sd(sigma):
    return math.sqrt(sd12(sigma) ** 2 * H_BLOCKS / 3 + 25 / (3 * H_BLOCKS))


def _t3_cdf(t):
    return 0.5 + (t / (math.sqrt(3) * (1 + t * t / 3)) + math.atan(t / math.sqrt(3))) / math.pi


def _t3_ppf(p):
    lo, hi = -1e4, 1e4
    for _ in range(200):
        mid = (lo + hi) / 2
        if _t3_cdf(mid) < p:
            lo = mid
        else:
            hi = mid
    return (lo + hi) / 2


def draws(kind, sigma):
    """(η in ticks, weight) pairs; weights sum to 1."""
    sd = eta_sd(sigma)
    if kind == "normal":
        x, w = np.polynomial.hermite_e.hermegauss(32)
        w = w / w.sum()
        return [(float(xi) * sd, float(wi)) for xi, wi in zip(x, w)]
    n = 64
    return [(_t3_ppf((i + 0.5) / n) / math.sqrt(3.0) * sd, 1.0 / n) for i in range(n)]


def sqrt_at_frac(t):
    """sqrt price at a fractional tick."""
    b = math.floor(t)
    return int(sqrt_at(b) * 1.0001 ** ((t - b) / 2))


def attack(h, r, ref, d_signed, lo, k, band=BAND, push_sqrt=None, tl=None, th=None):
    """push -> place -> revert. Returns the Sybil's P&L, the NAVs at r and the holdings before/after the action."""
    sr = sqrt_at(r)
    pool = R.Pool(sr, FEE)
    if h.L:
        pool.modify("vault", h.lo, h.hi, h.L)
    third = int(k * (h.L if h.L else 10 ** 17))
    if third:
        pool.modify("lp", tl, th, third)
    nav0 = R.value(h, sr)[0]
    ain, aout, zfo = pool.push_to(push_sqrt if push_sqrt is not None else sqrt_at(r + d_signed))
    syb0, syb1 = (-ain, aout) if zfo else (aout, -ain)
    tick_spot = pool.tick
    if R.placement_check(lo, lo + W, tick_spot, ref, band, W, SPACING):
        return None
    hb = h.copy()
    if hb.L:
        _, _, f0, f1 = pool.modify("vault", hb.lo, hb.hi, 0)
        hb.idle0 += f0
        hb.idle1 += f1
    before = hb.copy()
    s_spot = pool.s
    if hb.L:
        a0, a1, _, _ = pool.modify("vault", hb.lo, hb.hi, -hb.L)
        hb = R.Holdings(hb.idle0 - a0, hb.idle1 - a1)
    L, after, dl = R.placement_size(hb, lo, lo + W, pool.s, sqrt_at(ref))
    if dl < R.WAD // 2 or L == 0:
        return None
    pool.modify("vault", lo, lo + W, L)
    ain, aout, zfo = pool.push_to(sr)
    if zfo:
        syb0, syb1 = syb0 - ain, syb1 + aout
    else:
        syb0, syb1 = syb0 + aout, syb1 - ain
    _, _, f0, f1 = pool.modify("vault", lo, lo + W, 0)
    hf = after.copy()
    hf.idle0 += f0
    hf.idle1 += f1
    nav1 = R.value(hf, sr)[0]
    sybil = R.q(syb0, sr) + syb1 if syb0 >= 0 else syb1 - R.mul_div_up(-syb0, sr * sr, 1 << 192)
    return dict(sybil=sybil, nav0=nav0, nav1=nav1, before=before, after=after, s_spot=s_spot, tick_spot=tick_spot)


def loss_at(a, p):
    na = R.value(a["after"], p)[0]
    return R.value(a["before"], p)[0] - na, na


def charge_at(a, prices):
    best, navp = 0, R.value(a["after"], a["s_spot"])[0]
    for p in prices:
        loss, na = loss_at(a, p)
        if loss > best:
            best, navp = loss, na
    return best, navp


def net_of(a, charge, navp):
    return coalition_net(dict(sybil=a["sybil"], nav0=a["nav0"], nav1=a["nav1"], charge=charge, navp=navp),
                         R.WAD, F_CAP)


def c60(a, r, ref, sq_charge, sq_navp, dr):
    """Expected net over η and the luckiest net. Legs: pre-move spot (= the pushed spot, worst case), spot, ref, markout."""
    exp, best = 0.0, None
    t = a["tick_spot"]
    for eta, w in dr:
        x = min(max(r + eta, t - CLAMP), t + CLAMP)
        loss, na = loss_at(a, sqrt_at_frac(x))
        ch, nv = (loss, na) if loss > sq_charge else (sq_charge, sq_navp)
        n = net_of(a, ch, nv)
        exp += w * n
        best = n if best is None or n > best else best
    return exp, best


def sweep(h, r, ref, dr_n, dr_t):
    """Best cycle net per rule over d, direction, k and range. C60 is evaluated only where sq > 0: its legs are a
    superset of sq's, so its net is never above sq's on the same attack."""
    out = dict(sq=0, c60=0.0, c60_t3=0.0, c60_lucky=0, n_att=0, n_pos_sq=0, d_sq=None)
    for d in DS:
        for sg in (1, -1):
            if abs(r + sg * d - ref) > BAND:
                continue
            for k in KS:
                tl, th = third_range(r, sg * d)
                cand = candidates(r, r + sg * d, ref)
                cache = {lo: attack(h, r, ref, sg * d, lo, k, tl=tl, th=th) for lo in cand}
                legal = [lo for lo in cand if cache[lo]]
                if legal:
                    top = max(legal, key=lambda lo: cache[lo]["sybil"])
                    for lo in range(top - 30, top + 40, SPACING):
                        if lo not in cache:
                            cache[lo] = attack(h, r, ref, sg * d, lo, k, tl=tl, th=th)
                atts = [a for a in cache.values() if a]
                e = floor_sp(r + sg * d)
                for lo_e, ps in ((e, sqrt_at(e) + 1), (e + SPACING - W, sqrt_at(e + SPACING) - 1)):
                    a = attack(h, r, ref, sg * d, lo_e, k, push_sqrt=ps, tl=tl, th=th)
                    if a:
                        atts.append(a)
                for a in atts:
                    out["n_att"] += 1
                    ch, nv = charge_at(a, [a["s_spot"], sqrt_at(ref)])
                    n = net_of(a, ch, nv)
                    if n > out["sq"]:
                        out["sq"], out["d_sq"] = n, d
                    if n <= 0:
                        continue
                    out["n_pos_sq"] += 1
                    en, lucky = c60(a, r, ref, ch, nv, dr_n)
                    et, _ = c60(a, r, ref, ch, nv, dr_t)
                    out["c60"] = max(out["c60"], en)
                    out["c60_t3"] = max(out["c60_t3"], et)
                    out["c60_lucky"] = max(out["c60_lucky"], lucky)
    return out


def run(args):
    seed, sigma = args
    assert seed < 100, "fit seeds only"
    dr_n, dr_t = draws("normal", sigma), draws("t3", sigma)
    rng = random.Random(seed * 7919 + int(sigma * 100))
    rows = []
    for variant in ("exit", "in"):
        h, r, _ = sample_state(seed, sigma, exit_state=(variant == "exit"))
        for ell in lags(seed, sigma, LAGS_PER_STATE):
            if variant == "exit":
                sgn = 1 if r >= h.hi else -1          # the TWAP trails the move that left the range
            else:
                sgn = rng.choice((1, -1))
            ref = r - sgn * abs(ell)
            nav = R.value(h, sqrt_at(r))[0]
            res = sweep(h, r, ref, dr_n, dr_t)
            rows.append(dict(seed=seed, sigma=sigma, v=variant, lag=sgn * abs(ell), nav=nav, **res))
    return rows


def summarise(rows):
    lines = []
    for sg in sorted({r["sigma"] for r in rows}):
        lines.append(f"\nσ {sg}  (bound {BOUND[sg] * 100:.1f}% NAV/yr; t3 stress bound {2 * BOUND[sg] * 100:.1f}%)")
        for key, lab in (("sq", "sq (control)"), ("c60", "C60"), ("c60_t3", "C60 t3 stress"),
                         ("c60_lucky", "C60 luckiest draw (info)")):
            tot = [0.0, 0.0, 0.0]
            parts = []
            for v in ("exit", "in"):
                g = [r for r in rows if r["sigma"] == sg and r["v"] == v]
                xs = [n_opps(v, sg) * max(0, r[key]) / r["nav"] for r in g]
                m, lo, hi = mean_ci(xs)
                tot = [tot[0] + m, tot[1] + lo, tot[2] + hi]
                parts.append(f"{v} {m * 100:.3f}% ({sum(1 for x in xs if x > 0)}/{len(xs)} cycles > 0)")
            lines.append(f"  {lab:26s}: {tot[0] * 100:.3f}% [90% CI {tot[1] * 100:.3f}, {tot[2] * 100:.3f}]  " + "; ".join(parts))
        g = [r for r in rows if r["sigma"] == sg]
        mx = max(r["c60"] / r["nav"] for r in g)
        mxt = max(r["c60_t3"] / r["nav"] for r in g)
        lines.append(f"  max cycle E[net] C60 {mx * 1e4:.4f} bp, t3 {mxt * 1e4:.4f} bp of NAV; attacks evaluated "
                     f"{sum(r['n_att'] for r in g)}, with sq net > 0 {sum(r['n_pos_sq'] for r in g)}")
    return "\n".join(lines)


if __name__ == "__main__":
    n = int(sys.argv[1]) if len(sys.argv) > 1 else 100
    procs = int(sys.argv[2]) if len(sys.argv) > 2 else 9
    jobs = [(s, sg) for sg in (0.6, 1.2) for s in range(n)]
    assert max(s for s, _ in jobs) < 100
    t0 = time.time()
    rows = []
    with mp.Pool(procs) as pool:
        for i, rr in enumerate(pool.imap_unordered(run, jobs)):
            rows.extend(rr)
            print(f"{i + 1}/{len(jobs)} {time.time() - t0:.0f}s", flush=True, file=sys.stderr)
    os.makedirs(OUT, exist_ok=True)
    with open(os.path.join(OUT, "m2d_rate_fit.json"), "w") as fh:
        json.dump(rows, fh, default=str)
    s = f"m2d_rate (fit seeds 0–{n - 1}, {len(rows)} cycles, {time.time() - t0:.0f} s)" + summarise(rows)
    print(s)
    with open(os.path.join(OUT, "m2d_rate_fit.txt"), "w") as fh:
        fh.write(s + "\n")
