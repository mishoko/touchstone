#!/usr/bin/env python3
"""M-2d rate lane, corrected: the push free to the fence, waits up to the cure window, deep-spot offsets with tails
and lags paired with the path; plus the swap lanes (a stale-price `rebalanceSwap` and a pre-swap before a placement,
each into the Sybil's just-in-time liquidity).

PLACEMENT LANE. The coalition (the curator plus a Sybil with ordering control) pushes the pool price d ticks from the
market price r, the curator re-places the vault at the pushed spot (any legal W-wide range containing spot and the
reference, plus the two single-sided edge placements) and the Sybil swaps the price back to r. Everything is valued
at r; the curator holds the NAV-cap fraction of the supply and pays the charge by a share burn (`n4.coalition_net`).
- d ∈ {5, 10, 15, 20, 30, …, 120}, both directions, third-party depth k ∈ {0, 1};
- the reference lag is paired with the state: in an exit state the 10-minute TWAP trails the move that took the
  market out of the vault's range (the lag points back toward the old range); in range its sign is random; its
  magnitude is the TWAP lag of a 1-minute GBM path (`m2d.lags`).

SWAP LANES (the Sybil is the swap's counterparty through just-in-time liquidity, so no fee reaches the vault):
- (a) `rebalanceSwap` of Δx = 2% of NAV at a pool price D ticks behind the market (the reference lagging at least as
  far), D ∈ {5, 10, …, 60}; the Sybil unwinds at the market. Linear valuation of the fill at the stale price (the
  just-in-time liquidity is deep, so the fill has no slippage).
- (b) the same swap as the pre-swap of a placement attack (one action, charged once over both steps: the loss at any
  price p is additive over the two steps at that p).
- Opportunities: `MAX_SWAP_EPOCH` (5% of NAV per epoch, 52 epochs a year) bounds the swapped volume: 2.6 NAV a year,
  i.e. 130 swaps of 2% a year; (a) and (b) share that budget.

CHARGE RULES evaluated on the same physical attack (the attack's outcome does not depend on the charge):
- sq    the current rule: the worse of {spot, reference} (the positive control: its rate must be > 0);
- C120  the candidate repair: the worse of {pre-move spot, spot, reference, p_mk − FEE_TICKS, p_mk + FEE_TICKS}, where
        the pre-move spot is the spot read before the vault's own move (the colluder may pre-set it anywhere on its
        push path; the lane takes the worst case for the defence: the pushed spot itself), and p_mk is the deep pool's
        TWAP over ten 12-second blocks starting one block after the action's block (no clamp). The settlement
        adds extra = min(max(0, loss at the worse of p_mk ± FEE_TICKS − the immediate charge), cap·NAV_before), the
        cap being the fixed reserve taken at the action (default 6e-4 of NAV).
  The markout is drawn CONDITIONAL on what the colluder sees: the deep pool's closing tick follows
  u_k = clip(u_{k−1}, m_k − 5, m_k + 5) and, in the action block, the colluder acts after a move that left the deep
  pool at the stale edge of its band (u_1 = m_1 − 5 on the side the colluder exploits); η = mean(u_2 … u_11) − m_1.
  A fixed Monte Carlo sample of η per σ and distribution (GBM; Student-t3 at the same sd as the stress) is reused
  across attacks; the colluder takes the better sign of the bias. The colluder commits before the markout exists, so
  an attack's net is its expectation over η; the luckiest draw is reported for information.
- s16   the fee-aware settlement of the specification (§3.4): the same immediate legs; the settlement is
        `R.settle_extra` at the integer markout tick floor(r + bias·η) (the window mean floored), legs tick_mk − FEE_TICKS,
        tick_mk and tick_mk + FEE_TICKS + 1, each holding valued with the arbitrage fee its position earns from s0 (before)
        or sEnd (after) to the leg, the leg chosen by g/nb, and the burn re-priced in shares: the coalition's net uses the
        total burn burn₀ + burnS against a fixed supply S_act = n4.S_TOT (burn₀ at NAV' = the smallest NAV_after over the
        immediate legs). s0 = sEnd = the pushed spot (the defence's worst case, as for C120). The ablation adds one element
        at a time (v0: C120 at exact fractional ticks; v1: the integer floor; v2: the upper leg + 1; v3: the centre leg;
        v4: the share burn; s16: the fee terms). An attack the status quo does not let through cannot profit under s16:
        its burn is ≥ burn₀ ≥ the status quo's burn (more legs, a lower burn price), so only sq-positive attacks are
        evaluated, as for C120.
A cycle's net is the best net over all legal attacks (0 if the coalition abstains). Placement rate per year =
`m2d.n_opps` (once per COOLDOWN in range; per exit the best of 10 COOLDOWN slots) × E[max(0, cycle net)], summed
over exit and in-range lanes. Swap lanes: (a) 2.6 NAV/yr × the best D's per-unit net; (b) 130/yr × the best cycle's
net (the colluder spends its swap budget on its best opportunities). Total = placements + max(a, b).

Bound: ≤ 0.1% of NAV per year at σ 60% and ≤ 0.6% at σ 120%; the Student-t3 stress ≤ 2× the bound.
Fit seeds 0–99: python3 model/cells/m2d_rate.py [n_seeds] [procs] [cap (default 6e-4)] [clamp (diagnostic; default none)].
Re-gate (held-out seeds, the shipped parameters only): python3 model/cells/m2d_rate.py --seeds FIRST:END [procs]
(writes output/$M2D_OUT/m2d_rate_<FIRST>-<END>.json/.txt, default output/s16; seeds 300–499 are judged, 100–299
reported; C120 and the status quo are printed as before, the s16 rule after them). M2D_DIFF=1 on fit seeds adds the
exact-tick C120 on the pre-swap lane and asserts the generalised rule equal to `R.settle_extra` on every evaluation.
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
from n4 import coalition_net, candidates, F_CAP, FEE, S_TOT  # noqa: E402
from m2d import lags, third_range, n_opps  # noqa: E402

DS = (5, 10, 15, 20, 30, 40, 50, 60, 70, 80, 90, 100, 110, 120)
DS_SWAP = (5, 10, 15, 20, 30, 40, 50, 60)
KS = (0, 1)
BAND = 60
FEE_TICKS = 5
CAP = 6e-4              # the fixed settlement reserve, a fraction of NAV before the action
WINDOW = 10            # blocks in the markout window
N_ETA = 2000
SWAP_FRAC = 0.02
SWAPS_PER_YEAR = 0.05 * 52 / SWAP_FRAC     # 130
BOUND = {0.6: 1e-3, 1.2: 6e-3}
LAGS_PER_STATE = 3
GRID_STEP = 0.25
DIFF = os.environ.get("M2D_DIFF") == "1"   # fit seeds: the exact-tick C120 (v0) on lane (b) and the R.settle_extra check
ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
OUT = os.path.join(ROOT, "output", "s4")
OUT_TAG = os.environ.get("M2D_OUT", "s16")   # the re-gate's output directory under output/


def sd12(sigma):
    return sigma * math.sqrt(12 / (365 * 86400)) / math.log(1.0001)


def eta_sample(sigma, dist, n=N_ETA, seed=0):
    """η = mean of the deep pool's closing ticks over the window minus the market at the action, given that the
    action block closed at the stale band edge (u_1 = m_1 − 5). Negative mean: the bias points to the stale side."""
    rng = np.random.default_rng(1_000_003 + int(sigma * 100) * 7 + (0 if dist == "gbm" else 1) + seed * 13)
    T = WINDOW
    inc = rng.standard_normal((n, T)) if dist == "gbm" else rng.standard_t(3, (n, T)) / np.sqrt(3.0)
    m = np.concatenate([np.zeros((n, 1)), np.cumsum(inc * sd12(sigma), axis=1)], axis=1)   # m[:,0] = m_1 = 0
    u = np.empty_like(m)
    u[:, 0] = -float(FEE_TICKS)
    for k in range(1, T + 1):
        u[:, k] = np.clip(u[:, k - 1], m[:, k] - FEE_TICKS, m[:, k] + FEE_TICKS)
    return u[:, 1:].mean(axis=1)                   # blocks 2..11: the window starts one block after the action


def sqrt_at_frac(t):
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
    return dict(sybil=sybil, nav0=nav0, nav1=nav1, before=before, after=after, s_spot=s_spot, tick_spot=tick_spot,
                pre_idle=hb)


def loss_at(a, p):
    na = R.value(a["after"], p)[0]
    return R.value(a["before"], p)[0] - na, na


def charge_at(a, prices):
    """The worse-of charge over `prices` and the NAV after at the argmax (spot on a tie)."""
    best, navp = 0, R.value(a["after"], a["s_spot"])[0]
    for p in prices:
        loss, na = loss_at(a, p)
        if loss > best:
            best, navp = loss, na
    return best, navp


def net_of(a, charge, navp, extra_gain=0, nav_shift=0):
    return coalition_net(dict(sybil=a["sybil"] + extra_gain, nav0=a["nav0"], nav1=a["nav1"] - nav_shift,
                              charge=charge, navp=navp), R.WAD, F_CAP)


class Swap:
    """A fill of Δx at the stale spot s into the Sybil's just-in-time liquidity (fee to the Sybil). `sell0` = the
    vault sells token0 (the pool price is below the market)."""

    def __init__(self, amount, sell0, s):
        self.amount, self.sell0, self.s = amount, sell0, s
        fee_keep = 10 ** 6 - FEE
        if sell0:
            self.got = R.q(amount, s) * fee_keep // 10 ** 6
        else:
            self.got = R.mul_div(amount, 1 << 192, s * s) * fee_keep // 10 ** 6

    def loss(self, p):
        """The vault's value change at price p (positive = loss)."""
        if self.sell0:
            return R.q(self.amount, p) - self.got
        return self.amount - R.q(self.got, p)


def c120(a, r, ref, legs_imm, eta, sgn, swap=None, cap=CAP, clamp=None):
    """Expected coalition net over η (sign sgn on the bias), the luckiest net, and the share of draws in which the
    settlement cap binds. The markout legs are p_mk ± FEE_TICKS; the extra charge at settlement is capped at
    cap·NAV_before (the fixed reserve); an optional clamp of p_mk to spot ± clamp is a diagnostic."""
    t = a["tick_spot"]
    imm, imm_nav = legs_imm
    rel = r + sgn * eta - t
    if clamp is not None:
        rel = np.clip(rel, -clamp, clamp)
    lo_x, hi_x = math.floor(rel.min()) - FEE_TICKS - 1, math.ceil(rel.max()) + FEE_TICKS + 1
    xs = np.arange(lo_x, hi_x + GRID_STEP / 2, GRID_STEP)
    ls = []
    for x in xs:
        p = sqrt_at_frac(t + float(x))
        l, _ = loss_at(a, p)
        if swap:
            l += swap.loss(p)
        ls.append(l)
    ls = np.array(ls, dtype=float)
    mk = np.maximum(np.interp(rel - FEE_TICKS, xs, ls), np.interp(rel + FEE_TICKS, xs, ls))
    extra = np.maximum(0.0, mk - imm)
    cap_w = cap * float(R.value(a["before"], a["s_spot"])[0]) if cap is not None else float("inf")
    binds = extra > cap_w
    ch = imm + np.minimum(extra, cap_w)
    gain = swap.loss(sqrt_at(r)) if swap else 0           # the Sybil's gain on the fill, valued at the market
    nets = [net_of(a, int(c), max(imm_nav, 1), gain, gain) for c in ch]
    return float(np.mean(nets)), float(max(nets)), float(binds.mean())


# ----------------------------------------------------------------------------- the fee-aware settlement (s16 rule)
# The specification's settlement (§3.4, fee-aware legs): `R.settle_extra` evaluated at the integer markout tick
# tick_mk = floor(market + bias·η) (the window mean floored, as `tryWindow` returns it), with s0 = the pushed spot (the
# defence's worst case, as for C120) and sEnd = the spot the action leaves. The charge is a share burn against a fixed
# supply S_act = n4.S_TOT: burn₀ = ceilDiv(charge₀·S, NAV') with NAV' the smallest NAV_after over {s0, spot, reference},
# the reserve's shares ceilDiv((charge₀ + reserve)·S, NAV') − burn₀, and the settlement burn burnS of §3.4.
# `settle_var` generalises the rule for the ablation and the differential check (every flag off reproduces C120 at
# exact fractional ticks); with every flag on and no extra loss term it is asserted equal to `R.settle_extra`.
S_ACT = S_TOT
RULE_FULL = dict(int_tick=True, up1=True, centre=True, fees=True, shares=True)
VARIANTS = (  # the ablation, in order: each adds one element of the s16 rule
    ("v0_s05x", dict(int_tick=False, up1=False, centre=False, fees=False, shares=False)),
    ("v1_int", dict(int_tick=True, up1=False, centre=False, fees=False, shares=False)),
    ("v2_up1", dict(int_tick=True, up1=True, centre=False, fees=False, shares=False)),
    ("v3_centre", dict(int_tick=True, up1=True, centre=True, fees=False, shares=False)),
    ("v4_shares", dict(int_tick=True, up1=True, centre=True, fees=False, shares=True)),
    ("s16", RULE_FULL),
)


def _p(t):
    return sqrt_at(t) if float(t).is_integer() else sqrt_at_frac(float(t))


def legs_of(ctx, extra_loss=None):
    """A per-tick cache of the leg terms (NAV before, NAV after, F before, F after, the pre-swap's loss) shared by every
    rule evaluated on one attack."""
    cache = {}
    b, a = ctx["before"], ctx["after"]

    def leg(t):
        v = cache.get(t)
        if v is None:
            p = _p(t)
            v = (R.value(b, p)[0], R.value(a, p)[0], R.arb_fee(b, ctx["s0"], p, True, FEE),
                 R.arb_fee(a, ctx["sEnd"], p, False, FEE), extra_loss(p) if extra_loss is not None else 0)
            cache[t] = v
        return v
    return leg


def leg_val(ctx, extra_loss=None):
    """As `legs_of` without the fee terms (the rules that do not use them; avoids computing F)."""
    cache = {}
    b, a = ctx["before"], ctx["after"]

    def leg(t):
        v = cache.get(t)
        if v is None:
            p = _p(t)
            v = (R.value(b, p)[0], R.value(a, p)[0], 0, 0, extra_loss(p) if extra_loss is not None else 0)
            cache[t] = v
        return v
    return leg


def settle_var(ctx, leg, tick_mk, cfg):
    """One settlement under the rule `cfg` at markout tick `tick_mk` (an integer when cfg['int_tick']). Returns
    (total burn in shares or None, total charge value or None, cap binds). `ctx`: c0, nav_imm (NAV_after at the
    immediate charge's leg, the C120 burn price), burn0, reserve, reserve_shares; `leg(t)` the leg terms at tick t
    (a pre-swap's additive loss at p (lane b) is subtracted from NAV_after at every leg)."""
    up = FEE_TICKS + (1 if cfg["up1"] else 0)
    ticks = (tick_mk - FEE_TICKS, tick_mk, tick_mk + up) if cfg["centre"] else (tick_mk - FEE_TICKS, tick_mk + up)
    best = None
    for t in ticks:
        vb, va, fb, fa, xl = leg(t)
        nb, na = (vb + fb, va + fa - xl) if cfg["fees"] else (vb, va - xl)
        g = nb - na
        if cfg["shares"]:
            if best is None or (g > 0 and (best[0] <= 0 or g * best[1] > best[0] * nb)):
                best = (g, nb)
        elif best is None or g > best[0]:
            best = (g, nb)
    g, nb = best
    loss = max(0, g)
    if not cfg["shares"]:   # C120: extra = min(reserve, max(0, loss − charge₀)), charged in value
        ex = max(0, loss - ctx["c0"])
        return None, ctx["c0"] + min(ex, ctx["reserve"]), ex > ctx["reserve"]
    if loss == 0:
        return ctx["burn0"], None, False
    req = -((-loss * S_ACT) // nb) if nb > 0 else None
    want = ctx["reserve_shares"] if req is None else max(0, req - ctx["burn0"])
    return ctx["burn0"] + min(ctx["reserve_shares"], want), None, want > ctx["reserve_shares"]


def make_ctx(before, after, s0, s_end, c0, nav_imm, nav_burn, reserve_base):
    """The action's record: burn₀ at NAV' = nav_burn, the reserve on reserve_base = NAV_before(spot) and its shares."""
    reserve = R.mul_div(int(round(CAP * R.WAD)), reserve_base, R.WAD)
    burn0 = -((-c0 * S_ACT) // nav_burn) if c0 else 0
    tot = -((-(c0 + reserve) * S_ACT) // nav_burn)
    return dict(before=before, after=after, s0=s0, sEnd=s_end, c0=c0, nav_imm=nav_imm, burn0=burn0, reserve=reserve,
                reserve_shares=tot - burn0)


def net_shares(a, burn, gain=0, shift=0):
    """`n4.coalition_net` with the charge given as a burn in shares (fixed supply S_TOT, the curator holding F_CAP)."""
    B = F_CAP * S_TOT // R.WAD
    nav1 = a["nav1"] - shift
    cur = (B - burn) * nav1 // (S_TOT - burn) - B * a["nav0"] // S_TOT if B >= burn else \
        -burn * nav1 // (S_TOT - burn) - B * a["nav0"] // S_TOT
    return a["sybil"] + gain + cur


def c16(a, r, ctx, eta, sgn, cfg, leg, swap=None, check=False):
    """Expected coalition net over η, the luckiest net and the share of draws on which the reserve caps the settlement,
    under the rule `cfg`. Integer rules are evaluated once per distinct tick_mk = floor(r + sgn·η)."""
    gain = swap.loss(sqrt_at(r)) if swap else 0
    if cfg["int_tick"]:
        tk = np.floor(r + sgn * eta).astype(np.int64)
        uniq, inv = np.unique(tk, return_inverse=True)
    else:
        uniq, inv = r + sgn * eta, np.arange(len(eta))
    vals, binds = [], []
    for t in uniq:
        t = int(t) if cfg["int_tick"] else float(t)
        burn, ch, bd = settle_var(ctx, leg, t, cfg)
        if check and cfg is RULE_FULL and swap is None:
            pend = dict(s0=ctx["s0"], sEnd=ctx["sEnd"], charge0=ctx["c0"], reserve=ctx["reserve"], burn0=ctx["burn0"],
                        sAct=S_ACT)
            _, _, bs = R.settle_extra(ctx["before"], ctx["after"], pend, t, FEE_TICKS, FEE, ctx["reserve_shares"])
            assert burn == ctx["burn0"] + bs, ("settle_var differs from R.settle_extra", t, burn, ctx["burn0"] + bs)
        n = net_shares(a, burn, gain, gain) if burn is not None else net_of(a, int(ch), max(ctx["nav_imm"], 1), gain, gain)
        vals.append(n)
        binds.append(bd)
    nets = np.array(vals, dtype=float)[inv]
    return float(np.mean(nets)), float(nets.max()), float(np.array(binds, dtype=float)[inv].mean())


def sweep(h, r, ref, etas, cap=CAP, clamp=None, diff=False):
    """Best cycle net per rule over d, direction, k and range, for the placement lane and the pre-swap lane (b)."""
    out = dict(sq=0, c=0.0, c_t3=0.0, c_lucky=0, sq_b=0, c_b=0.0, c_b_t3=0.0, n_att=0, n_pos_sq=0, n_pos_sq_b=0,
               bind_sum=0.0, bind_n=0, bind_b_sum=0.0, bind_b_n=0,
               s16_t3=0.0, s16_lucky=0.0, s16_b_t3=0.0, s16_bind_sum=0.0, s16_bind_b_sum=0.0, v0_diff=0.0, v0_b_diff=0.0)
    for name, _ in VARIANTS:
        out[name] = 0.0
        out[name + "_b"] = 0.0
        out[name + "_sig"] = -1e30        # the largest signed E[net] over the sq-positive attacks (the margin)
        out[name + "_b_sig"] = -1e30
    out["c_sig"] = out["c_b_sig"] = -1e30
    nav_r = R.value(h, sqrt_at(r))[0]
    ds = sorted(set(DS) | set(range(130, BAND + abs(r - ref) + 10, 10)))   # every legal push: up to the fence beyond the lag
    assert max(ds) >= BAND + abs(r - ref), "the push grid must reach the guard"
    for d in ds:
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
                    s_ref = sqrt_at(ref)
                    sgn_bias = -1 if a["tick_spot"] < r else 1     # the bias that helps: toward the pushed spot
                    # placement only
                    l_s, n_s = loss_at(a, a["s_spot"])
                    l_r, n_r = loss_at(a, s_ref)
                    imm = (l_s, n_s) if l_s >= l_r else (l_r, n_r)
                    imm = (max(0, imm[0]), imm[1])
                    n = net_of(a, imm[0], imm[1])
                    out["sq"] = max(out["sq"], n)
                    if n > 0:
                        # the s16 record: s0 = sEnd = the pushed spot; NAV' the smallest NAV_after over {s0, spot, ref}
                        ctx = make_ctx(a["before"], a["after"], a["s_spot"], a["s_spot"], imm[0], imm[1], min(n_s, n_r),
                                       R.value(a["before"], a["s_spot"])[0])
                        out["n_pos_sq"] += 1
                        for key, dist in (("c", "gbm"), ("c_t3", "t3")):
                            en, lucky, cl = c120(a, r, ref, imm, etas[dist], sgn_bias, cap=cap, clamp=clamp)
                            out[key] = max(out[key], en)
                            if dist == "gbm":
                                out["c_lucky"] = max(out["c_lucky"], lucky)
                                out["bind_sum"] += cl
                                out["bind_n"] += 1
                                c120_en = en
                                out["c_sig"] = max(out["c_sig"], en)
                        if cap == CAP and clamp is None:
                            leg = legs_of(ctx)
                            for name, cfg in VARIANTS:
                                en, lucky, cl = c16(a, r, ctx, etas["gbm"], sgn_bias, cfg, leg, check=diff)
                                out[name] = max(out[name], en)
                                out[name + "_sig"] = max(out[name + "_sig"], en)
                                if name == "v0_s05x":
                                    out["v0_diff"] = max(out["v0_diff"], abs(en - c120_en) / nav_r)
                                if name == "s16":
                                    out["s16_lucky"] = max(out["s16_lucky"], lucky)
                                    out["s16_bind_sum"] += cl
                            en, _, _ = c16(a, r, ctx, etas["t3"], sgn_bias, RULE_FULL, leg)
                            out["s16_t3"] = max(out["s16_t3"], en)
                    # (b) a 2% pre-swap at the pushed (stale) spot into the Sybil's JIT liquidity, then the placement
                    sell0 = a["tick_spot"] < r
                    amt = SWAP_FRAC * nav_r
                    pre = a["pre_idle"]
                    if sell0:
                        amt0 = int(amt * (1 << 192) / (a["s_spot"] ** 2))
                        if amt0 > pre.idle0:
                            continue
                        sw = Swap(amt0, True, a["s_spot"])
                    else:
                        if int(amt) > pre.idle1:
                            continue
                        sw = Swap(int(amt), False, a["s_spot"])
                    gain = sw.loss(sqrt_at(r))
                    ls_, lr_ = l_s + sw.loss(a["s_spot"]), l_r + sw.loss(s_ref)
                    imm_b = (ls_, n_s) if ls_ >= lr_ else (lr_, n_r)
                    imm_b = (max(0, imm_b[0]), imm_b[1])
                    nb = net_of(a, imm_b[0], imm_b[1], gain, gain)
                    out["sq_b"] = max(out["sq_b"], nb)
                    if nb > 0:
                        out["n_pos_sq_b"] += 1
                        for key, dist in (("c_b", "gbm"), ("c_b_t3", "t3")):
                            en, _, bb = c120(a, r, ref, imm_b, etas[dist], sgn_bias, swap=sw, cap=cap, clamp=clamp)
                            out[key] = max(out[key], en)
                            if dist == "gbm":
                                out["bind_b_sum"] += bb
                                out["bind_b_n"] += 1
                                c120_en = en
                                out["c_b_sig"] = max(out["c_b_sig"], en)
                        if cap == CAP and clamp is None:
                            # the pre-swap is part of the action: NAV_after(p) falls by its loss at p at every leg
                            nav_burn = min(n_s - sw.loss(a["s_spot"]), n_r - sw.loss(s_ref))
                            ctx_b = make_ctx(a["before"], a["after"], a["s_spot"], a["s_spot"], imm_b[0], imm_b[1],
                                             nav_burn, R.value(a["before"], a["s_spot"])[0])
                            leg = legs_of(ctx_b, sw.loss)
                            for name, cfg in VARIANTS:
                                if name == "v0_s05x" and not diff:
                                    continue
                                en, _, cl = c16(a, r, ctx_b, etas["gbm"], sgn_bias, cfg, leg, swap=sw)
                                out[name + "_b"] = max(out[name + "_b"], en)
                                out[name + "_b_sig"] = max(out[name + "_b_sig"], en)
                                if name == "v0_s05x":
                                    out["v0_b_diff"] = max(out["v0_b_diff"], abs(en - c120_en) / nav_r)
                                if name == "s16":
                                    out["s16_bind_b_sum"] += cl
                            en, _, _ = c16(a, r, ctx_b, etas["t3"], sgn_bias, RULE_FULL, leg, swap=sw)
                            out["s16_b_t3"] = max(out["s16_b_t3"], en)
    return out


def swap_lane_a(sigma, etas, cap=CAP, clamp=None):
    """Per unit of swapped value (ticks of 1e-4): sq and C120 nets for the best D (linear valuation). The cap per
    unit of swapped value is cap / SWAP_FRAC (as ticks of 1e-4)."""
    res = {}
    cap_t = cap / SWAP_FRAC / 1e-4 if cap is not None else float("inf")
    for dist, eta in etas.items():
        best_c, best_sq, best_d, bind = -1e9, -1e9, None, 0.0
        for D in DS_SWAP:
            g = D + FEE_TICKS                                        # the vault's loss at the market (ticks)
            sq = g - FEE_TICKS                                       # charge at spot = the fee; ref (≤ spot) less
            rel = D + eta                                            # p_mk − spot (η biased to the stale side)
            if clamp is not None:
                rel = np.clip(rel, -clamp, clamp)
            extra = np.maximum(0.0, rel + 2 * FEE_TICKS - FEE_TICKS)
            ch = FEE_TICKS + np.minimum(extra, cap_t)
            c = float(np.mean(g - ch))
            if c > best_c:
                best_c, best_d, bind = c, D, float(np.mean(extra > cap_t))
            best_sq = max(best_sq, sq)
        res[dist] = (best_sq, best_c, best_d, bind)
    return res


def swap_lane_a16(etas, cap=CAP):
    """Lane (a) under the s16 rule, per unit of swapped value (ticks of 1e-4): the markout tick floored to an integer
    (tick_mk − spot = floor(D + η), spot an integer tick), legs tick_mk − FEE_TICKS, tick_mk and tick_mk + FEE_TICKS + 1.
    The vault's loss on the fill is linear in the leg's tick, so the upper leg is the worst. The fee terms are left out:
    the position is unchanged by the swap and the fill has no slippage (sEnd = s0), so F(before) − F(after) ≥ 0 (the
    before term rounds up at γ + 2 pips, the credit rounds down less its margins): leaving it out can only overstate
    the colluder's net. The re-priced burn values the total burn at ≥ the loss at the leg (to first order in the swap's
    2% of NAV), i.e. the charge is max(charge₀, min(loss_mk, charge₀ + cap))."""
    res = {}
    cap_t = cap / SWAP_FRAC / 1e-4
    for dist, eta in etas.items():
        best_c, best_d, bind = -1e9, None, 0.0
        for D in DS_SWAP:
            g = D + FEE_TICKS
            rel = np.floor(D + eta)
            loss_mk = rel + FEE_TICKS + 1 + FEE_TICKS
            extra = np.maximum(0.0, loss_mk - FEE_TICKS)
            ch = FEE_TICKS + np.minimum(extra, cap_t)
            c = float(np.mean(g - ch))
            if c > best_c:
                best_c, best_d, bind = c, D, float(np.mean(extra > cap_t))
        res[dist] = (best_c, best_d, bind)
    return res


def run(args):
    seed, sigma, cap, clamp = args
    assert seed < 100 or (cap == CAP and clamp is None), "held-out seeds run the shipped parameters only"
    es = seed if seed >= 100 else 0       # held-out seeds draw their own η sample (fit seeds reuse sample 0)
    etas = {"gbm": eta_sample(sigma, "gbm", seed=es), "t3": eta_sample(sigma, "t3", seed=es)}
    rng = random.Random(seed * 7919 + int(sigma * 100))
    rows = []
    for variant in ("exit", "in"):
        h, r, _ = sample_state(seed, sigma, exit_state=(variant == "exit"))
        for ell in lags(seed, sigma, LAGS_PER_STATE):
            sgn = (1 if r >= h.hi else -1) if variant == "exit" else rng.choice((1, -1))
            ref = r - sgn * abs(ell)
            nav = R.value(h, sqrt_at(r))[0]
            res = sweep(h, r, ref, etas, cap, clamp, diff=seed < 100 and DIFF)
            rows.append(dict(seed=seed, sigma=sigma, v=variant, lag=sgn * abs(ell), nav=nav, **res))
    return rows


def summarise(rows, cap=CAP, clamp=None):
    lines = []
    for sg in sorted({r["sigma"] for r in rows}):
        etas = {"gbm": eta_sample(sg, "gbm", 200000), "t3": eta_sample(sg, "t3", 200000)}
        lines.append(f"\nσ {sg}  (bound {BOUND[sg] * 100:.1f}% NAV/yr; t3 stress bound {2 * BOUND[sg] * 100:.1f}%)")
        for dist in ("gbm", "t3"):
            e = etas[dist]
            lines.append(f"  η ({dist}): mean {e.mean():+.2f} ticks, sd {e.std():.2f}, P(|η| > 40) {np.mean(np.abs(e) > 40):.1e}")
        place = {}
        for key, lab in (("sq", "placements sq (control)"), ("c", "placements C120"), ("c_t3", "placements C120 t3"),
                         ("c_lucky", "placements C120 luckiest (info)")):
            tot = [0.0, 0.0, 0.0]
            parts = []
            for v in ("exit", "in"):
                g = [r for r in rows if r["sigma"] == sg and r["v"] == v]
                xs = [n_opps(v, sg) * max(0, r[key]) / r["nav"] for r in g]
                m, lo, hi = mean_ci(xs)
                tot = [tot[0] + m, tot[1] + lo, tot[2] + hi]
                parts.append(f"{v} {m * 100:.3f}% ({sum(1 for x in xs if x > 0)}/{len(xs)} > 0)")
            place[key] = tot
            lines.append(f"  {lab:32s}: {tot[0] * 100:.3f}% [90% CI {tot[1] * 100:.3f}, {tot[2] * 100:.3f}]  " + "; ".join(parts))
        g = [r for r in rows if r["sigma"] == sg]
        cl = sum(r["bind_sum"] for r in g) / max(1, sum(r["bind_n"] for r in g))
        clb = sum(r["bind_b_sum"] for r in g) / max(1, sum(r["bind_b_n"] for r in g))
        lines.append(f"  placements: attacks {sum(r['n_att'] for r in g)}, sq > 0 {sum(r['n_pos_sq'] for r in g)}; "
                     f"max cycle E[net] C120 {max(r['c'] / r['nav'] for r in g) * 1e4:.4f} bp (t3 "
                     f"{max(r['c_t3'] / r['nav'] for r in g) * 1e4:.4f}); cap binds on {cl * 100:.1f}% of draws "
                     f"(over sq-positive attacks)")
        a = swap_lane_a(sg, etas, cap, clamp)
        sw = {}
        for dist in ("gbm", "t3"):
            sq_u, c_u, d_best, bind_share = a[dist]
            sw[dist] = (SWAPS_PER_YEAR * SWAP_FRAC * sq_u * 1e-4, SWAPS_PER_YEAR * SWAP_FRAC * max(0.0, c_u) * 1e-4)
            lines.append(f"  swap (a) rebalanceSwap {dist}: sq {sw[dist][0] * 100:.3f}%/yr (per unit {sq_u:.1f} ticks at D "
                         f"{DS_SWAP[-1]}); C120 {sw[dist][1] * 100:.3f}%/yr (best E[net]/Δx {c_u:+.2f} ticks at D {d_best}; "
                         f"cap binds at that D on {bind_share * 100:.1f}%)")
        bmax = {}
        for key in ("sq_b", "c_b", "c_b_t3"):
            vals = sorted((max(0, r[key]) / r["nav"] for r in g), reverse=True)
            top = vals[:max(1, len(vals) // 10)]
            bmax[key] = SWAPS_PER_YEAR * max(vals)
            lines.append(f"  swap (b) pre-swap + placement {key}: best-cycle rate {bmax[key] * 100:.3f}%/yr, top-decile mean "
                         f"{SWAPS_PER_YEAR * sum(top) / len(top) * 100:.3f}%/yr, cycles > 0 {sum(1 for x in vals if x > 0)}/{len(vals)}")
        lines.append(f"  pre-swap attacks with sq > 0: {sum(r['n_pos_sq_b'] for r in g)}; cap binds on {clb * 100:.1f}% of their draws")
        tot_sq = place["sq"][0] + max(sw["gbm"][0], bmax["sq_b"])
        tot_c = place["c"][0] + max(sw["gbm"][1], bmax["c_b"])
        tot_t = place["c_t3"][0] + max(sw["t3"][1], bmax["c_b_t3"])
        lines.append(f"  TOTAL (placements + max(a, b)): sq {tot_sq * 100:.3f}%/yr; C120 {tot_c * 100:.3f}%/yr "
                     f"(bound {BOUND[sg] * 100:.1f}); C120 t3 {tot_t * 100:.3f}%/yr (bound {2 * BOUND[sg] * 100:.1f})")
    return "\n".join(lines)


def summarise16(rows, ablation=True):
    """The s16 rule (fee-aware settlement) next to C120 on the same attacks and η draws, and the ablation."""
    lines = ["\nThe fee-aware settlement (s16 rule: R.settle_extra at the floored integer markout tick, burn in shares at "
             f"S_act = {S_ACT:.0e}); same attacks and η draws as C120"]
    for sg in sorted({r["sigma"] for r in rows}):
        g = [r for r in rows if r["sigma"] == sg]
        lines.append(f"σ {sg}  (bound {BOUND[sg] * 100:.1f}% NAV/yr; t3 {2 * BOUND[sg] * 100:.1f}%)")

        def place(key):
            tot = [0.0, 0.0, 0.0]
            parts = []
            for v in ("exit", "in"):
                xs = [n_opps(v, sg) * max(0, r[key]) / r["nav"] for r in g if r["v"] == v]
                m, lo, hi = mean_ci(xs)
                tot = [tot[0] + m, tot[1] + lo, tot[2] + hi]
                parts.append(f"{v} {sum(1 for x in xs if x > 0)}/{len(xs)} > 0")
            return tot, "; ".join(parts)

        def sig_max(key):
            xs = [r[key] / r["nav"] * 1e4 for r in g if r[key] > -1e29]
            return f"{max(xs):+.4f} bp" if xs else "none (no sq-positive attack)"

        def best_b(key):
            return SWAPS_PER_YEAR * max(max(0, r[key]) / r["nav"] for r in g)
        etas = {"gbm": eta_sample(sg, "gbm", 200000), "t3": eta_sample(sg, "t3", 200000)}
        a16 = swap_lane_a16(etas)
        a05 = swap_lane_a(sg, etas)
        names = [n for n, _ in VARIANTS] if ablation else ["s16"]
        for name in names:
            tot, parts = place(name)
            bb = best_b(name + "_b")
            mx = max(r[name] / r["nav"] for r in g) * 1e4
            mxb = max(r[name + "_b"] / r["nav"] for r in g) * 1e4
            sig, sigb = sig_max(name + "_sig"), sig_max(name + "_b_sig")
            lines.append(f"  {name:10s}: placements {tot[0] * 100:.3f}% [90% CI {tot[1] * 100:.3f}, {tot[2] * 100:.3f}] ({parts}); "
                         f"max cycle E[net] {mx:.4f} bp (largest signed attack E[net] {sig}); pre-swap (b) best-cycle "
                         f"{bb * 100:.3f}%/yr (max cycle E[net] {mxb:.4f} bp; largest signed {sigb})")
        lines.append(f"  C120 (s05) largest signed attack E[net]: placements {sig_max('c_sig')}; pre-swap {sig_max('c_b_sig')}")
        sw16 = {d: SWAPS_PER_YEAR * SWAP_FRAC * max(0.0, a16[d][0]) * 1e-4 for d in a16}
        for d in ("gbm", "t3"):
            lines.append(f"  swap (a) {d}: s16 {sw16[d] * 100:.3f}%/yr (best E[net]/Δx {a16[d][0]:+.2f} ticks at D {a16[d][1]}; "
                         f"cap binds {a16[d][2] * 100:.1f}%) | C120 best E[net]/Δx {a05[d][1]:+.2f} ticks at D {a05[d][2]}")
        p16, _ = place("s16")
        pt3, _ = place("s16_t3")
        tot16 = p16[0] + max(sw16["gbm"], best_b("s16_b"))
        tot16t = pt3[0] + max(sw16["t3"], best_b("s16_b_t3"))
        n_pos = sum(r["n_pos_sq"] for r in g)
        n_pos_b = sum(r["n_pos_sq_b"] for r in g)
        lk, _ = place("s16_lucky")
        lines.append(f"  s16 t3: placements {pt3[0] * 100:.3f}%; max cycle E[net] {max(r['s16_t3'] / r['nav'] for r in g) * 1e4:.4f} bp; "
                     f"pre-swap (b) {best_b('s16_b_t3') * 100:.3f}%/yr")
        lines.append(f"  s16 luckiest draw (info): placements {lk[0] * 100:.3f}%/yr; max per cycle "
                     f"{max(r['s16_lucky'] / r['nav'] for r in g) * 1e4:.4f} bp")
        lines.append(f"  s16 reserve cap binds on {sum(r['s16_bind_sum'] for r in g) / max(1, n_pos) * 100:.2f}% of draws "
                     f"(placements, {n_pos} sq-positive attacks) and {sum(r['s16_bind_b_sum'] for r in g) / max(1, n_pos_b) * 100:.2f}% "
                     f"(pre-swap, {n_pos_b})")
        if ablation:
            lines.append(f"  differential v0 (exact ticks, every s16 element off) vs C120 (0.25-tick grid): max |ΔE[net]| per "
                         f"attack {max(r['v0_diff'] for r in g) * 1e4:.2e} bp (placements), {max(r['v0_b_diff'] for r in g) * 1e4:.2e} bp (pre-swap)")
        lines.append(f"  TOTAL s16 (placements + max(a, b)): {tot16 * 100:.3f}%/yr (bound {BOUND[sg] * 100:.1f}); t3 "
                     f"{tot16t * 100:.3f}%/yr (bound {2 * BOUND[sg] * 100:.1f})")
    return "\n".join(lines)


if __name__ == "__main__" and len(sys.argv) > 2 and sys.argv[1] == "--seeds":
    first, end = (int(x) for x in sys.argv[2].split(":"))
    procs = int(sys.argv[3]) if len(sys.argv) > 3 else 9
    jobs = [(s, sg, CAP, None) for sg in (0.6, 1.2) for s in range(first, end)]
    t0 = time.time()
    rows = []
    with mp.Pool(procs) as pool:
        for i, rr in enumerate(pool.imap_unordered(run, jobs)):
            rows.extend(rr)
            if (i + 1) % 20 == 0:
                print(f"{i + 1}/{len(jobs)} {time.time() - t0:.0f}s", flush=True, file=sys.stderr)
    out = os.path.join(ROOT, "output", OUT_TAG)
    os.makedirs(out, exist_ok=True)
    with open(os.path.join(out, f"m2d_rate_{first}-{end - 1}.json"), "w") as fh:
        json.dump(rows, fh, default=str)
    s = (f"m2d_rate re-gate, reserve {CAP:g}·NAV (seeds {first}–{end - 1}, {len(rows)} cycles, {time.time() - t0:.0f} s)"
         + summarise(rows, CAP, None) + "\n" + summarise16(rows, ablation=first < 100))
    print(s)
    with open(os.path.join(out, f"m2d_rate_{first}-{end - 1}.txt"), "w") as fh:
        fh.write(s + "\n")
    sys.exit(0)

if __name__ == "__main__":
    n = int(sys.argv[1]) if len(sys.argv) > 1 else 100
    procs = int(sys.argv[2]) if len(sys.argv) > 2 else 9
    cap = float(sys.argv[3]) if len(sys.argv) > 3 else CAP               # the settlement reserve (fraction of NAV)
    clamp = int(sys.argv[4]) if len(sys.argv) > 4 and sys.argv[4] != "none" else None   # diagnostic only
    tag = ("" if cap == CAP else f"_cap{cap:g}") + ("" if clamp is None else f"_clamp{clamp}")
    jobs = [(s, sg, cap, clamp) for sg in (0.6, 1.2) for s in range(n)]
    assert max(j[0] for j in jobs) < 100
    t0 = time.time()
    rows = []
    with mp.Pool(procs) as pool:
        for i, rr in enumerate(pool.imap_unordered(run, jobs)):
            rows.extend(rr)
            print(f"{i + 1}/{len(jobs)} {time.time() - t0:.0f}s", flush=True, file=sys.stderr)
    os.makedirs(OUT, exist_ok=True)
    with open(os.path.join(OUT, f"m2d_rate_fit_v3{tag}.json"), "w") as fh:
        json.dump(rows, fh, default=str)
    s = f"m2d_rate C120, reserve cap {cap:g}·NAV, clamp {clamp} (fit seeds 0–{n - 1}, {len(rows)} cycles, {time.time() - t0:.0f} s)" + summarise(rows, cap, clamp)
    print(s)
    with open(os.path.join(OUT, f"m2d_rate_fit_v3{tag}.txt"), "w") as fh:
        fh.write(s + "\n")
