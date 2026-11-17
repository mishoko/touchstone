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
- fee_aware   the fee-aware settlement of the specification (§3.4): the same immediate legs; the settlement is
        `R.settle_extra` at the integer markout tick tick_mk = floor(mean_k floor(r + bias·u_k)) (the reference pool's
        closing tick is an integer per block and `tryWindow` floors the window mean), legs tick_mk − FEE_TICKS, tick_mk
        and tick_mk + FEE_TICKS + 1, each holding valued with the arbitrage fee its position earns from s0 (before) or
        sEnd (after) to the leg, the leg chosen by g/nb, and the burn re-priced in shares: the coalition's net uses the
        total burn burn₀ + burnS against a fixed supply S_act = n4.S_TOT (burn₀ at NAV' = the smallest NAV_after over the
        immediate legs). The ablation adds one element at a time (v0: C120 at exact fractional ticks; v1: the integer
        floor; v2: the upper leg + 1; v3: the centre leg; v4: the share burn; fee_aware: the fee terms); its column fixes
        s0 = sEnd = the pushed spot (the C120-era convention, kept for the differential).
        THE s0 SEARCH (column fee_aware_s0, the verdict column). Under the fee-aware rule s0 also sets F(before, s0, ·),
        which shrinks as s0 approaches the leg, so the pushed spot is no longer the defence's worst case. For every attack
        (range, push target, k) the Sybil instead pushes only to s0 ∈ {the pushed spot, the reference, the old range's
        edges, r − FEE_TICKS, r + FEE_TICKS}, each clipped to the push path [r, pushed spot] (no guard reads s0: the
        fence and containment run on the post-move spot), the vault removes its old position at s0 and its own `moveTo`
        (§3.6) carries spot from s0 to the pushed spot: across an empty region at k = 0 (free), across the third party's
        liquidity at k = 1 (the vault pays the swap from idle, the fee to the third party; the move's input valued at the
        reference must fit `MAX_SWAP_ACTION`·NAV_before(reference) and idle, else the action reverts). The immediate legs
        are then {spot, s0, reference} (spot first, then s0, then the reference on ties) and NAV' the smallest NAV_after
        over the three. The coalition takes the best s0 per attack (the column is the maximum over the base attack and
        every searched s0). The sq filter stays valid: the status quo (legs {spot, reference}) is evaluated on each
        searched attack and burn₀ over three legs is ≥ the status quo's burn.
        An attack the status quo does not let through cannot profit under the fee-aware rule: its burn is ≥ burn₀ ≥ the
        status quo's burn (more legs, a lower burn price), so only sq-positive attacks are evaluated, as for C120.
A cycle's net is the best net over all legal attacks (0 if the coalition abstains). Placement rate per year =
`m2d.n_opps` (once per COOLDOWN in range; per exit the best of 10 COOLDOWN slots) × E[max(0, cycle net)], summed
over exit and in-range lanes. Swap lanes: (a) 2.6 NAV/yr × the best D's per-unit net; (b) 130/yr × the best cycle's
net (the colluder spends its swap budget on its best opportunities). Total = placements + max(a, b).

Bound: ≤ 0.1% of NAV per year at σ 60% and ≤ 0.6% at σ 120%; the Student-t3 stress ≤ 2× the bound.
Lane (b) and the s0 search: lane (b) keeps s0 = sEnd = the pushed spot, and its F(after) term uses the placement-only
after holding (the position placed from the un-swapped idle), although in the action the pre-swap runs before the
placement and so changes L: the additive approximation lane (b) inherits from C120 (the loss at p is the placement's
plus the swap's at p).

Fit seeds 0–99: python3 model/cells/m2d_rate.py [n_seeds] [procs] [cap (default 6e-4)] [clamp (diagnostic; default none)].
Re-gate (held-out seeds, the shipped parameters only):
  python3 model/cells/m2d_rate.py --seeds FIRST:END [procs] [--out DIR]
writes DIR/m2d_rate_<FIRST>-<END-1>.json/.txt (DIR: --out, else $M2D_OUT_DIR, else output/fee_aware); seeds 300–499 are
judged, 100–299 reported. The JSON holds the per-cycle rows and a `meta` record: the git commit (`git rev-parse HEAD`
and whether the tree was dirty), the command line, the wall time, and per σ the largest signed attack E[net] under
C120 and the fee-aware rule (fixed s0 and searched s0), the s0 search's gain over the pushed spot and the v0 differential.
Slices run separately are summarised together with
  python3 model/cells/m2d_rate.py --merge OUT_STEM part1.json part2.json ...
M2D_DIFF=1 on fit seeds adds the exact-tick C120 on the pre-swap lane and asserts the generalised rule equal to
`R.settle_extra` on every evaluation (placements, searched s0 and lane (b)).
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
MAX_SWAP_ACTION = R.MAINNET.MAX_SWAP_ACTION   # the vault's own move shares the per-action swap cap (§3.6)
ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
OUT = os.path.join(ROOT, "output", "s4")
OUT_DIR = os.environ.get("M2D_OUT_DIR", os.path.join(ROOT, "output", "fee_aware"))   # the re-gate's output directory


def sd12(sigma):
    return sigma * math.sqrt(12 / (365 * 86400)) / math.log(1.0001)


def eta_sample(sigma, dist, n=N_ETA, seed=0, blocks=False):
    """η = mean of the deep pool's closing ticks over the window minus the market at the action, given that the
    action block closed at the stale band edge (u_1 = m_1 − 5). Negative mean: the bias points to the stale side.
    With `blocks`, also the integer markout offsets per bias sign: {sgn: floor(mean_k floor(sgn·u_k))} (int64), so that
    tick_mk = r + offset[sgn] for an integer market tick r (each block's close is an integer tick; `tryWindow` floors
    the window mean)."""
    rng = np.random.default_rng(1_000_003 + int(sigma * 100) * 7 + (0 if dist == "gbm" else 1) + seed * 13)
    T = WINDOW
    inc = rng.standard_normal((n, T)) if dist == "gbm" else rng.standard_t(3, (n, T)) / np.sqrt(3.0)
    m = np.concatenate([np.zeros((n, 1)), np.cumsum(inc * sd12(sigma), axis=1)], axis=1)   # m[:,0] = m_1 = 0
    u = np.empty_like(m)
    u[:, 0] = -float(FEE_TICKS)
    for k in range(1, T + 1):
        u[:, k] = np.clip(u[:, k - 1], m[:, k] - FEE_TICKS, m[:, k] + FEE_TICKS)
    ub = u[:, 1:]                                   # blocks 2..11: the window starts one block after the action
    eta = ub.mean(axis=1)
    if not blocks:
        return eta
    return eta, int_offsets(ub)


def int_offsets(ub):
    """{sgn: floor(mean_k floor(sgn·u_k))} for a (draws × blocks) matrix of continuous closes relative to the market."""
    nb = ub.shape[1]
    return {s: np.floor_divide(np.floor(s * ub).astype(np.int64).sum(axis=1), nb) for s in (1, -1)}


def sqrt_at_frac(t):
    b = math.floor(t)
    return int(sqrt_at(b) * 1.0001 ** ((t - b) / 2))


def attack(h, r, ref, d_signed, lo, k, band=BAND, push_sqrt=None, tl=None, th=None, s0_sqrt=None):
    """push -> place -> revert. Returns the Sybil's P&L, the NAVs at r and the holdings before/after the action.
    `s0_sqrt` (the s0 search): the Sybil pushes only to s0, the vault removes its old position there and its own
    `moveTo` carries spot to the push target (an exact-output swap from idle across whatever third-party liquidity lies
    between; it reverts on short idle or above `MAX_SWAP_ACTION`·NAV_before(reference) at the reference)."""
    sr = sqrt_at(r)
    pool = R.Pool(sr, FEE)
    if h.L:
        pool.modify("vault", h.lo, h.hi, h.L)
    third = int(k * (h.L if h.L else 10 ** 17))
    if third:
        pool.modify("lp", tl, th, third)
    nav0 = R.value(h, sr)[0]
    target = push_sqrt if push_sqrt is not None else sqrt_at(r + d_signed)
    if s0_sqrt is None:
        s0_sqrt = target
    ain, aout, zfo = pool.push_to(s0_sqrt)
    syb0, syb1 = (-ain, aout) if zfo else (aout, -ain)
    if s0_sqrt == target:
        tick_spot = pool.tick
        if R.placement_check(lo, lo + W, tick_spot, ref, band, W, SPACING):
            return None
    hb = h.copy()
    if hb.L:
        _, _, f0, f1 = pool.modify("vault", hb.lo, hb.hi, 0)
        hb.idle0 += f0
        hb.idle1 += f1
    before = hb.copy()
    s0 = pool.s
    if hb.L:
        a0, a1, _, _ = pool.modify("vault", hb.lo, hb.hi, -hb.L)
        hb = R.Holdings(hb.idle0 - a0, hb.idle1 - a1)
    move_in = 0
    if pool.s != target:   # the vault's own move (§3.6): the old position is already removed
        ain, aout, zfo = pool.push_to(target)
        if pool.s != target:
            return None                                  # Move
        hb = R.Holdings(hb.idle0 - ain, hb.idle1 + aout) if zfo else R.Holdings(hb.idle0 + aout, hb.idle1 - ain)
        if hb.idle0 < 0 or hb.idle1 < 0:
            return None                                  # SwapCap (idle short)
        s_ref = sqrt_at(ref)
        move_in = R.mul_div_up(ain, s_ref * s_ref, 1 << 192) if zfo else ain
        if move_in * R.WAD > MAX_SWAP_ACTION * R.value(before, s_ref)[0]:
            return None                                  # SwapCap (the per-action cap)
        tick_spot = pool.tick
        if R.placement_check(lo, lo + W, tick_spot, ref, band, W, SPACING):
            return None
    s_spot = pool.s
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
                pre_idle=hb, s0=s0, lo=lo, push_sqrt=push_sqrt, move_in=move_in)


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

    def after(self, h):
        """The holding h with the fill applied to its idle (the exact form of the additive loss, for the check)."""
        if self.sell0:
            return R.Holdings(h.idle0 - self.amount, h.idle1 + self.got, h.L, h.lo, h.hi)
        return R.Holdings(h.idle0 + self.got, h.idle1 - self.amount, h.L, h.lo, h.hi)


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


# ----------------------------------------------------------------------------- the fee-aware settlement
# The specification's settlement (§3.4, fee-aware legs): `R.settle_extra` evaluated at the integer markout tick
# tick_mk = floor(mean_k floor(market + bias·u_k)) (integer closes per block, the window mean floored as `tryWindow`
# returns it), with s0 the spot before the vault's own move and sEnd = the spot the action leaves. The charge is a share
# burn against a fixed supply S_act = n4.S_TOT: burn₀ = ceilDiv(charge₀·S, NAV') with NAV' the smallest NAV_after over
# {s0, spot, reference}, the reserve's shares ceilDiv((charge₀ + reserve)·S, NAV') − burn₀, and the settlement burn
# burnS of §3.4. `settle_var` generalises the rule for the ablation and the differential check (every flag off
# reproduces C120 at exact fractional ticks); with every flag on it is asserted equal to `R.settle_extra` (lane (b):
# on the after holding with the fill applied to idle, the additive leg within 1 wei of it).
S_ACT = S_TOT
RULE_FULL = dict(int_tick=True, up1=True, centre=True, fees=True, shares=True)
VARIANTS = (  # the ablation, in order: each adds one element of the fee-aware rule
    ("v0_exact", dict(int_tick=False, up1=False, centre=False, fees=False, shares=False)),
    ("v1_int", dict(int_tick=True, up1=False, centre=False, fees=False, shares=False)),
    ("v2_up1", dict(int_tick=True, up1=True, centre=False, fees=False, shares=False)),
    ("v3_centre", dict(int_tick=True, up1=True, centre=True, fees=False, shares=False)),
    ("v4_shares", dict(int_tick=True, up1=True, centre=True, fees=False, shares=True)),
    ("fee_aware", RULE_FULL),
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


CHECKS = {"settle_extra": 0, "settle_extra_b": 0}   # assertion counts (M2D_DIFF=1), reported by the smoke run


def check_settle(ctx, t, burn, swap=None):
    """Asserts `settle_var` under the full rule equal to `R.settle_extra` at tick t. With a pre-swap (lane (b)) the
    reference runs on the after holding with the fill applied to idle (`Swap.after`), the generalised rule is re-run on
    that holding, and the additive leg (NAV_after − the swap's loss) is asserted within 1 wei of it at every leg."""
    after = ctx["after"] if swap is None else swap.after(ctx["after"])
    pend = dict(s0=ctx["s0"], sEnd=ctx["sEnd"], charge0=ctx["c0"], reserve=ctx["reserve"], burn0=ctx["burn0"], sAct=S_ACT)
    _, _, bs = R.settle_extra(ctx["before"], after, pend, t, FEE_TICKS, FEE, ctx["reserve_shares"])
    if swap is None:
        assert burn == ctx["burn0"] + bs, ("settle_var differs from R.settle_extra", t, burn, ctx["burn0"] + bs)
        CHECKS["settle_extra"] += 1
        return
    ctx_x = dict(ctx, after=after)
    leg_x, leg_a = legs_of(ctx_x), legs_of(ctx, swap.loss)
    burn_x, _, _ = settle_var(ctx_x, leg_x, t, RULE_FULL)
    assert burn_x == ctx["burn0"] + bs, ("settle_var (lane b, exact after) differs from R.settle_extra", t, burn_x)
    for tt in (t - FEE_TICKS, t, t + FEE_TICKS + 1):
        va = leg_a(tt)
        vx = leg_x(tt)
        assert abs((va[1] - va[4]) - vx[1]) <= 1 and va[3] == vx[3], ("lane (b) additive leg off by > 1 wei", tt)
    CHECKS["settle_extra_b"] += 1


def c_fee_aware(a, r, ctx, eta, sgn, cfg, leg, swap=None, check=False, eta_int=None):
    """Expected coalition net over η, the luckiest net and the share of draws on which the reserve caps the settlement,
    under the rule `cfg`. Integer rules are evaluated once per distinct tick_mk = r + eta_int[sgn] (the floored mean of
    the per-block integer closes); fractional rules at r + sgn·η."""
    gain = swap.loss(sqrt_at(r)) if swap else 0
    if cfg["int_tick"]:
        tk = r + eta_int[sgn]
        uniq, inv = np.unique(tk, return_inverse=True)
    else:
        uniq, inv = r + sgn * eta, np.arange(len(eta))
    vals, binds = [], []
    for t in uniq:
        t = int(t) if cfg["int_tick"] else float(t)
        burn, ch, bd = settle_var(ctx, leg, t, cfg)
        if check and cfg is RULE_FULL:
            check_settle(ctx, t, burn, swap)
        n = net_shares(a, burn, gain, gain) if burn is not None else net_of(a, int(ch), max(ctx["nav_imm"], 1), gain, gain)
        vals.append(n)
        binds.append(bd)
    nets = np.array(vals, dtype=float)[inv]
    return float(np.mean(nets)), float(nets.max()), float(np.array(binds, dtype=float)[inv].mean())


def s0_candidates(h, r, ref, t_target):
    """The s0 search (S16F-01): (label, tick) for the reference, r ∓ FEE_TICKS and the old range's edges, each clipped
    to the push path [r, t_target]; the pushed spot itself is the base attack. Duplicates keep the first label."""
    a, b = sorted((r, t_target))
    xs = [("ref", ref), ("r-5", r - FEE_TICKS), ("r+5", r + FEE_TICKS)]
    if h.L:
        xs += [("old_lo", h.lo), ("old_hi", h.hi)]
    out, seen = [], set()
    for lab, x in xs:
        x = min(max(x, a), b)
        if x != t_target and x not in seen:
            seen.add(x)
            out.append((lab, x))
    return out


def imm_legs(a, s_ref):
    """The immediate charge over {spot, s0, reference} (spot first, then s0, then the reference; a later leg replaces
    only on a strictly larger loss), the NAV_after at its leg and NAV' = the smallest NAV_after over the three."""
    vals = [loss_at(a, p) for p in (a["s_spot"], a["s0"], s_ref)]
    best = vals[0]
    for v in vals[1:]:
        if v[0] > best[0]:
            best = v
    return max(0, best[0]), best[1], min(v[1] for v in vals)


def sq_net(a, s_ref):
    """The status-quo control's net (legs {spot, reference}) and its two legs."""
    l_s, n_s = loss_at(a, a["s_spot"])
    l_r, n_r = loss_at(a, s_ref)
    imm = (l_s, n_s) if l_s >= l_r else (l_r, n_r)
    imm = (max(0, imm[0]), imm[1])
    return net_of(a, imm[0], imm[1]), imm, (l_s, n_s), (l_r, n_r)


def sweep(h, r, ref, etas, cap=CAP, clamp=None, diff=False, etas_int=None):
    """Best cycle net per rule over d, direction, k and range, for the placement lane and the pre-swap lane (b); the
    fee-aware rule also with the s0 search (column fee_aware_s0)."""
    out = dict(sq=0, c=0.0, c_t3=0.0, c_lucky=0, sq_b=0, c_b=0.0, c_b_t3=0.0, n_att=0, n_pos_sq=0, n_pos_sq_b=0,
               bind_sum=0.0, bind_n=0, bind_b_sum=0.0, bind_b_n=0,
               fa_t3=0.0, fa_lucky=0.0, fa_b_t3=0.0, fa_bind_sum=0.0, fa_bind_b_sum=0.0, v0_diff=0.0, v0_b_diff=0.0,
               fee_aware_s0=0.0, fee_aware_s0_sig=-1e30, fa_s0_t3=0.0, s0_n_att=0, s0_n_pos_sq=0, s0_beats=0, s0_new_pos=0,
               s0_gain=-1e30, s0_gain_label=None, s0_wins={})
    for name, _ in VARIANTS:
        out[name] = 0.0
        out[name + "_b"] = 0.0
        out[name + "_sig"] = -1e30        # the largest signed E[net] over the sq-positive attacks (the margin)
        out[name + "_b_sig"] = -1e30
    out["c_sig"] = out["c_b_sig"] = -1e30
    nav_r = R.value(h, sqrt_at(r))[0]
    s_ref = sqrt_at(ref)
    full = cap == CAP and clamp is None
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
                    sgn_bias = -1 if a["tick_spot"] < r else 1     # the bias that helps: toward the pushed spot
                    # placement only
                    n, imm, (l_s, n_s), (l_r, n_r) = sq_net(a, s_ref)
                    out["sq"] = max(out["sq"], n)
                    base_fa = None
                    if n > 0:
                        # the fee-aware record with s0 = sEnd = the pushed spot; NAV' the smallest NAV_after over the legs
                        c0, nav_imm, nav_burn = imm_legs(a, s_ref)
                        assert (c0, nav_imm, nav_burn) == (imm[0], imm[1], min(n_s, n_r))
                        ctx = make_ctx(a["before"], a["after"], a["s0"], a["s_spot"], c0, nav_imm, nav_burn,
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
                        if full:
                            leg = legs_of(ctx)
                            for name, cfg in VARIANTS:
                                en, lucky, cl = c_fee_aware(a, r, ctx, etas["gbm"], sgn_bias, cfg, leg, check=diff,
                                                            eta_int=etas_int["gbm"])
                                out[name] = max(out[name], en)
                                out[name + "_sig"] = max(out[name + "_sig"], en)
                                if name == "v0_exact":
                                    out["v0_diff"] = max(out["v0_diff"], abs(en - c120_en) / nav_r)
                                if name == "fee_aware":
                                    out["fa_lucky"] = max(out["fa_lucky"], lucky)
                                    out["fa_bind_sum"] += cl
                                    base_fa = en
                                    out["fee_aware_s0"] = max(out["fee_aware_s0"], en)
                                    out["fee_aware_s0_sig"] = max(out["fee_aware_s0_sig"], en)
                            en, _, _ = c_fee_aware(a, r, ctx, etas["t3"], sgn_bias, RULE_FULL, leg, eta_int=etas_int["t3"])
                            out["fa_t3"] = max(out["fa_t3"], en)
                            out["fa_s0_t3"] = max(out["fa_s0_t3"], en)
                    if full:
                        # the s0 search: push only to s0, the vault's own move carries spot to the same target
                        t_target = R.get_tick_at_sqrt_price(a["push_sqrt"]) if a["push_sqrt"] is not None else r + sg * d
                        best_v = None
                        for lab, t0 in s0_candidates(h, r, ref, t_target):
                            av = attack(h, r, ref, sg * d, a["lo"], k, push_sqrt=a["push_sqrt"], tl=tl, th=th,
                                        s0_sqrt=sqrt_at(t0))
                            if av is None:
                                continue
                            out["s0_n_att"] += 1
                            nv, _, _, _ = sq_net(av, s_ref)
                            if nv <= 0:
                                continue                    # the fee-aware net ≤ the status quo's ≤ 0
                            out["s0_n_pos_sq"] += 1
                            sb = -1 if av["tick_spot"] < r else 1
                            c0, nav_imm, nav_burn = imm_legs(av, s_ref)
                            ctx_v = make_ctx(av["before"], av["after"], av["s0"], av["s_spot"], c0, nav_imm, nav_burn,
                                             R.value(av["before"], av["s_spot"])[0])
                            leg_v = legs_of(ctx_v)
                            en, _, _ = c_fee_aware(av, r, ctx_v, etas["gbm"], sb, RULE_FULL, leg_v, check=diff,
                                                   eta_int=etas_int["gbm"])
                            out["fee_aware_s0"] = max(out["fee_aware_s0"], en)
                            out["fee_aware_s0_sig"] = max(out["fee_aware_s0_sig"], en)
                            en_t, _, _ = c_fee_aware(av, r, ctx_v, etas["t3"], sb, RULE_FULL, leg_v, eta_int=etas_int["t3"])
                            out["fa_s0_t3"] = max(out["fa_s0_t3"], en_t)
                            if best_v is None or en > best_v[0]:
                                best_v = (en, lab)
                        if best_v is not None and n <= 0:
                            out["s0_new_pos"] += 1          # the status quo filters the pushed spot, not this s0
                        elif best_v is not None:            # a like-for-like comparison: both fee-aware E[net]
                            gain = (best_v[0] - base_fa) / nav_r
                            if gain > out["s0_gain"]:
                                out["s0_gain"], out["s0_gain_label"] = gain, best_v[1]
                            if best_v[0] > base_fa:
                                out["s0_beats"] += 1
                                out["s0_wins"][best_v[1]] = out["s0_wins"].get(best_v[1], 0) + 1
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
                        if full:
                            # the pre-swap is part of the action: NAV_after(p) falls by its loss at p at every leg
                            # (s0 = sEnd = the pushed spot; F(after) on the placement-only after holding, see the docstring)
                            nav_burn = min(n_s - sw.loss(a["s_spot"]), n_r - sw.loss(s_ref))
                            ctx_b = make_ctx(a["before"], a["after"], a["s0"], a["s_spot"], imm_b[0], imm_b[1],
                                             nav_burn, R.value(a["before"], a["s_spot"])[0])
                            leg = legs_of(ctx_b, sw.loss)
                            for name, cfg in VARIANTS:
                                if name == "v0_exact" and not diff:
                                    continue
                                en, _, cl = c_fee_aware(a, r, ctx_b, etas["gbm"], sgn_bias, cfg, leg, swap=sw,
                                                        check=diff, eta_int=etas_int["gbm"])
                                out[name + "_b"] = max(out[name + "_b"], en)
                                out[name + "_b_sig"] = max(out[name + "_b_sig"], en)
                                if name == "v0_exact":
                                    out["v0_b_diff"] = max(out["v0_b_diff"], abs(en - c120_en) / nav_r)
                                if name == "fee_aware":
                                    out["fa_bind_b_sum"] += cl
                            en, _, _ = c_fee_aware(a, r, ctx_b, etas["t3"], sgn_bias, RULE_FULL, leg, swap=sw,
                                                   eta_int=etas_int["t3"])
                            out["fa_b_t3"] = max(out["fa_b_t3"], en)
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


def swap_lane_a_fa(etas_int, cap=CAP):
    """Lane (a) under the fee-aware rule, per unit of swapped value (ticks of 1e-4): the markout tick an integer,
    tick_mk − spot = D + floor(mean_k floor(u_k)) (spot an integer tick, each block's close an integer tick; `etas_int`
    the offsets of `eta_sample(..., blocks=True)`, sign +1 as rel = D + η in `swap_lane_a`), legs tick_mk − FEE_TICKS,
    tick_mk and tick_mk + FEE_TICKS + 1. The vault's loss on the fill is linear in the leg's tick, so the upper leg is
    the worst. The fee terms are left out: the position is unchanged by the swap and the fill has no slippage
    (sEnd = s0), so F(before) − F(after) ≥ 0 (the before term rounds up at γ + 2 pips, the credit rounds down less its
    margins): leaving it out can only overstate the colluder's net. The re-priced burn values the total burn at ≥ the
    loss at the leg (to first order in the swap's 2% of NAV), i.e. the charge is max(charge₀, min(loss_mk, charge₀ +
    cap))."""
    res = {}
    cap_t = cap / SWAP_FRAC / 1e-4
    for dist, off in etas_int.items():
        best_c, best_d, bind = -1e9, None, 0.0
        for D in DS_SWAP:
            g = D + FEE_TICKS
            rel = (D + off[1]).astype(float)
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
    etas, etas_int = {}, {}
    for dist in ("gbm", "t3"):
        etas[dist], etas_int[dist] = eta_sample(sigma, dist, seed=es, blocks=True)
    rng = random.Random(seed * 7919 + int(sigma * 100))
    rows = []
    for variant in ("exit", "in"):
        h, r, _ = sample_state(seed, sigma, exit_state=(variant == "exit"))
        for ell in lags(seed, sigma, LAGS_PER_STATE):
            sgn = (1 if r >= h.hi else -1) if variant == "exit" else rng.choice((1, -1))
            ref = r - sgn * abs(ell)
            nav = R.value(h, sqrt_at(r))[0]
            res = sweep(h, r, ref, etas, cap, clamp, diff=seed < 100 and DIFF, etas_int=etas_int)
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


def sig_max_bp(g, key):
    """The largest signed E[net] (bp of NAV) of `key` over the cycles g; None if no sq-positive attack was evaluated."""
    xs = [r[key] / r["nav"] * 1e4 for r in g if r[key] > -1e29]
    return max(xs) if xs else None


def summarise_fa(rows, ablation=True):
    """The fee-aware settlement next to C120 on the same attacks and η draws, the s0 search, and the ablation."""
    lines = ["\nThe fee-aware settlement (R.settle_extra at the integer markout tick floor(mean of the per-block integer closes), "
             f"burn in shares at S_act = {S_ACT:.0e}); same attacks and η draws as C120"]
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
            x = sig_max_bp(g, key)
            return f"{x:+.4f} bp" if x is not None else "none (no sq-positive attack)"

        def best_b(key):
            return SWAPS_PER_YEAR * max(max(0, r[key]) / r["nav"] for r in g)
        etas, etas_int = {}, {}
        for dist in ("gbm", "t3"):
            etas[dist], etas_int[dist] = eta_sample(sg, dist, 200000, blocks=True)
        afa = swap_lane_a_fa(etas_int)
        a05 = swap_lane_a(sg, etas)
        names = [n for n, _ in VARIANTS] if ablation else ["fee_aware"]
        for name in names:
            tot, parts = place(name)
            bb = best_b(name + "_b")
            mx = max(r[name] / r["nav"] for r in g) * 1e4
            mxb = max(r[name + "_b"] / r["nav"] for r in g) * 1e4
            sig, sigb = sig_max(name + "_sig"), sig_max(name + "_b_sig")
            lines.append(f"  {name:10s}: placements {tot[0] * 100:.3f}% [90% CI {tot[1] * 100:.3f}, {tot[2] * 100:.3f}] ({parts}); "
                         f"max cycle E[net] {mx:.4f} bp (largest signed attack E[net] {sig}); pre-swap (b) best-cycle "
                         f"{bb * 100:.3f}%/yr (max cycle E[net] {mxb:.4f} bp; largest signed {sigb})")
        ps0, parts0 = place("fee_aware_s0")
        wins = {}
        for r in g:
            for k_, v_ in r["s0_wins"].items():
                wins[k_] = wins.get(k_, 0) + v_
        gains = [(r["s0_gain"], r["s0_gain_label"]) for r in g if r["s0_gain"] > -1e29]
        gmax = max(gains, default=(None, None))
        lines.append(f"  fee_aware_s0 (s0 searched; the verdict column): placements {ps0[0] * 100:.3f}% [90% CI {ps0[1] * 100:.3f}, "
                     f"{ps0[2] * 100:.3f}] ({parts0}); max cycle E[net] {max(r['fee_aware_s0'] / r['nav'] for r in g) * 1e4:.4f} bp "
                     f"(largest signed attack E[net] {sig_max('fee_aware_s0_sig')}; fixed s0 {sig_max('fee_aware_sig')})")
        lines.append(f"  s0 search: {sum(r['s0_n_att'] for r in g)} legal searched attacks, {sum(r['s0_n_pos_sq'] for r in g)} "
                     f"sq-positive; attacks whose pushed spot the status quo filters but a searched s0 does not: "
                     f"{sum(r['s0_new_pos'] for r in g)}; where both are evaluated, a searched s0 beat the pushed spot on "
                     f"{sum(r['s0_beats'] for r in g)} attacks {wins or ''}, largest gain "
                     + (f"{gmax[0] * 1e4:+.4f} bp ({gmax[1]})" if gmax[0] is not None else "none")
                     + " (best searched fee-aware E[net] − the pushed spot's)")
        lines.append(f"  C120 largest signed attack E[net]: placements {sig_max('c_sig')}; pre-swap {sig_max('c_b_sig')}")
        swfa = {d: SWAPS_PER_YEAR * SWAP_FRAC * max(0.0, afa[d][0]) * 1e-4 for d in afa}
        for d in ("gbm", "t3"):
            lines.append(f"  swap (a) {d}: fee_aware {swfa[d] * 100:.3f}%/yr (best E[net]/Δx {afa[d][0]:+.2f} ticks at D {afa[d][1]}; "
                         f"cap binds {afa[d][2] * 100:.1f}%) | C120 best E[net]/Δx {a05[d][1]:+.2f} ticks at D {a05[d][2]}")
        pt3, _ = place("fa_s0_t3")
        tot_fa = ps0[0] + max(swfa["gbm"], best_b("fee_aware_b"))
        tot_fat = pt3[0] + max(swfa["t3"], best_b("fa_b_t3"))
        n_pos = sum(r["n_pos_sq"] for r in g)
        n_pos_b = sum(r["n_pos_sq_b"] for r in g)
        lk, _ = place("fa_lucky")
        lines.append(f"  fee_aware t3 (s0 searched): placements {pt3[0] * 100:.3f}%; max cycle E[net] "
                     f"{max(r['fa_s0_t3'] / r['nav'] for r in g) * 1e4:.4f} bp; pre-swap (b) {best_b('fa_b_t3') * 100:.3f}%/yr")
        lines.append(f"  fee_aware luckiest draw (info, fixed s0): placements {lk[0] * 100:.3f}%/yr; max per cycle "
                     f"{max(r['fa_lucky'] / r['nav'] for r in g) * 1e4:.4f} bp")
        lines.append(f"  fee_aware reserve cap binds on {sum(r['fa_bind_sum'] for r in g) / max(1, n_pos) * 100:.2f}% of draws "
                     f"(placements, {n_pos} sq-positive attacks) and {sum(r['fa_bind_b_sum'] for r in g) / max(1, n_pos_b) * 100:.2f}% "
                     f"(pre-swap, {n_pos_b})")
        lines.append(f"  differential v0 (exact ticks, every fee-aware element off) vs C120 (0.25-tick grid): max |ΔE[net]| per "
                     f"attack {max(r['v0_diff'] for r in g) * 1e4:.2e} bp (placements), {max(r['v0_b_diff'] for r in g) * 1e4:.2e} bp "
                     f"(pre-swap{'' if ablation else '; computed with M2D_DIFF=1 on fit seeds only'})")
        lines.append(f"  TOTAL fee_aware (placements with s0 searched + max(a, b)): {tot_fa * 100:.3f}%/yr (bound {BOUND[sg] * 100:.1f}); t3 "
                     f"{tot_fat * 100:.3f}%/yr (bound {2 * BOUND[sg] * 100:.1f})")
    return "\n".join(lines)


def git_meta():
    """The commit the run executes (and whether tracked files had uncommitted changes)."""
    import subprocess
    try:
        head = subprocess.run(["git", "-C", ROOT, "rev-parse", "HEAD"], capture_output=True, text=True, check=True).stdout.strip()
        dirty = bool(subprocess.run(["git", "-C", ROOT, "status", "--porcelain", "--untracked-files=no"],
                                    capture_output=True, text=True, check=True).stdout.strip())
    except (OSError, subprocess.CalledProcessError):
        head, dirty = None, None
    return dict(commit=head, dirty=dirty)


def meta_of(rows, wall_s):
    """The traceability record of a run: commit, command, wall time and per σ the margins."""
    m = dict(git_meta(), argv=sys.argv, wall_s=round(wall_s, 1), cycles=len(rows), sigmas={})
    for sg in sorted({r["sigma"] for r in rows}):
        g = [r for r in rows if r["sigma"] == sg]
        gains = [r["s0_gain"] for r in g if r["s0_gain"] > -1e29]
        m["sigmas"][str(sg)] = dict(
            largest_signed_bp=dict(c120=sig_max_bp(g, "c_sig"), c120_preswap=sig_max_bp(g, "c_b_sig"),
                                   fee_aware_fixed_s0=sig_max_bp(g, "fee_aware_sig"),
                                   fee_aware_s0_searched=sig_max_bp(g, "fee_aware_s0_sig"),
                                   fee_aware_preswap=sig_max_bp(g, "fee_aware_b_sig")),
            s0_beats=sum(r["s0_beats"] for r in g), s0_gain_max_bp=max(gains) * 1e4 if gains else None,
            s0_new_pos=sum(r["s0_new_pos"] for r in g), s0_sq_pos=sum(r["s0_n_pos_sq"] for r in g),
            v0_diff_max_bp=max(r["v0_diff"] for r in g) * 1e4, v0_b_diff_max_bp=max(r["v0_b_diff"] for r in g) * 1e4,
            sq_pos_attacks=sum(r["n_pos_sq"] for r in g), cycles_fa_s0_pos=sum(1 for r in g if r["fee_aware_s0"] > 0))
    return m


def run_pool(jobs, procs, every=20):
    """Runs the jobs on `procs` workers; returns the rows, the wall time and the summed assertion counts."""
    t0 = time.time()
    rows, checks = [], {}
    with mp.Pool(procs) as pool:
        for i, (rr, ck) in enumerate(pool.imap_unordered(run_checked, jobs)):
            rows.extend(rr)
            for k_, v_ in ck.items():
                checks[k_] = checks.get(k_, 0) + v_
            if (i + 1) % every == 0 or i + 1 == len(jobs):
                print(f"{i + 1}/{len(jobs)} {time.time() - t0:.0f}s", flush=True, file=sys.stderr)
    return rows, time.time() - t0, checks


def run_checked(args):
    """`run` plus the assertion counts of this job (the counters live in the worker process)."""
    before = dict(CHECKS)
    rows = run(args)
    return rows, {k_: CHECKS[k_] - before[k_] for k_ in CHECKS}


if __name__ == "__main__" and len(sys.argv) > 3 and sys.argv[1] == "--merge":
    # python3 model/cells/m2d_rate.py --merge OUT_STEM part1.json part2.json ...: one summary over slices run separately
    # (each slice keeps its own meta: commit, command, wall time); writes OUT_STEM.json/.txt
    stem, parts = sys.argv[2], sys.argv[3:]
    rows, metas = [], []
    for p_ in parts:
        with open(p_) as fh:
            d_ = json.load(fh)
        rows.extend(d_["rows"])
        metas.append(dict(d_["meta"], file=os.path.abspath(p_)))
    commits = {m_["commit"] for m_ in metas}
    meta = meta_of(rows, sum(m_["wall_s"] for m_ in metas))
    meta.update(parts=metas, commits_agree=len(commits) == 1, any_dirty=any(m_["dirty"] for m_ in metas))
    seeds = sorted({r_["seed"] for r_ in rows})
    s = (f"m2d_rate re-gate (merged {len(parts)} slices), reserve {CAP:g}·NAV (seeds {seeds[0]}–{seeds[-1]}, {len(set(seeds))} "
         f"seeds, {len(rows)} cycles, {meta['wall_s']:.0f} s summed; commits {sorted(c_ or '?' for c_ in commits)}"
         f"{' (uncommitted changes in a slice)' if meta['any_dirty'] else ''})"
         + summarise(rows, CAP, None) + "\n" + summarise_fa(rows, ablation=seeds[0] < 100))
    with open(stem + ".json", "w") as fh:
        json.dump(dict(meta=meta, rows=rows), fh, default=str)
    with open(stem + ".txt", "w") as fh:
        fh.write(s + "\n")
    print(s)
    sys.exit(0)

if __name__ == "__main__" and len(sys.argv) > 2 and sys.argv[1] == "--seeds":
    args = sys.argv[2:]
    out = OUT_DIR
    if "--out" in args:
        j = args.index("--out")
        out = os.path.abspath(args[j + 1])
        args = args[:j] + args[j + 2:]
    first, end = (int(x) for x in args[0].split(":"))
    procs = int(args[1]) if len(args) > 1 else 2
    jobs = [(s, sg, CAP, None) for sg in (0.6, 1.2) for s in range(first, end)]
    rows, wall, checks = run_pool(jobs, procs)
    os.makedirs(out, exist_ok=True)
    meta = meta_of(rows, wall)
    meta["checks"] = checks
    s = (f"m2d_rate re-gate, reserve {CAP:g}·NAV (seeds {first}–{end - 1}, {len(rows)} cycles, {wall:.0f} s; commit "
         f"{meta['commit']}{' (uncommitted changes)' if meta['dirty'] else ''})"
         + summarise(rows, CAP, None) + "\n" + summarise_fa(rows, ablation=first < 100))
    with open(os.path.join(out, f"m2d_rate_{first}-{end - 1}.json"), "w") as fh:
        json.dump(dict(meta=meta, rows=rows), fh, default=str)
    print(s)
    with open(os.path.join(out, f"m2d_rate_{first}-{end - 1}.txt"), "w") as fh:
        fh.write(s + "\n")
    sys.exit(0)

if __name__ == "__main__":
    n = int(sys.argv[1]) if len(sys.argv) > 1 else 100
    procs = int(sys.argv[2]) if len(sys.argv) > 2 else 2
    cap = float(sys.argv[3]) if len(sys.argv) > 3 else CAP               # the settlement reserve (fraction of NAV)
    clamp = int(sys.argv[4]) if len(sys.argv) > 4 and sys.argv[4] != "none" else None   # diagnostic only
    tag = ("" if cap == CAP else f"_cap{cap:g}") + ("" if clamp is None else f"_clamp{clamp}")
    jobs = [(s, sg, cap, clamp) for sg in (0.6, 1.2) for s in range(n)]
    assert max(j[0] for j in jobs) < 100
    rows, wall, checks = run_pool(jobs, procs, every=1)
    os.makedirs(OUT, exist_ok=True)
    meta = meta_of(rows, wall)
    meta["checks"] = checks
    with open(os.path.join(OUT, f"m2d_rate_fit_v3{tag}.json"), "w") as fh:
        json.dump(dict(meta=meta, rows=rows), fh, default=str)
    s = f"m2d_rate C120, reserve cap {cap:g}·NAV, clamp {clamp} (fit seeds 0–{n - 1}, {len(rows)} cycles, {wall:.0f} s)" + summarise(rows, cap, clamp)
    if cap == CAP and clamp is None:
        s += "\n" + summarise_fa(rows, ablation=True)
    s += f"\nassertions executed (M2D_DIFF={int(DIFF)}): {checks}"
    print(s)
    with open(os.path.join(OUT, f"m2d_rate_fit_v3{tag}.txt"), "w") as fh:
        fh.write(s + "\n")
