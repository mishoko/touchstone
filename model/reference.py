#!/usr/bin/env python3
"""Touchstone reference model v2.

An independent executable model of the Touchstone specification, written from the specification text
only (never from the Solidity). It covers:

- valuation: the quote form `q`/`val`, position amounts per Appendix A, NAV after collect-first;
- the execution ledger: the worse-of charge over {spot, reference}, `p*`, and the one-price burn and
  solvency gate in shares;
- placement: the pure `Placement.check` / `size` library;
- the mode machine: `Ladder.next` / `Ladder.allowed`;
- the bank: cohorts, youngest-first burn, bond, vesting, `maturedPending`, the forfeiture drip, claim,
  retire, release;
- the hook: the snapshot procedure, the reference module and cache, the breach clock and
  latch, the pending-breach queue, registration and arming;
- the vault: the breach-first checkpoint with the vest gate and its live leg, base accrual and
  deferred minting, epilogue re-bases, curator actions, deposits, withdrawals and seed;
- repair C (the markout-settled charge): the pre-move spot leg s0, the vault's own move to `moveTo`,
  `maxCharge`, the fixed reserve and its encumbrance in the bank, the pending record, settlement as
  prologue step 0 with the hook's window read, and the markout legs p_mk ± FEE_TICKS.

Run `python3 model/reference.py --selfcheck` for the property checks and `--vectors` to emit the golden
vectors into `test/vectors/`.
"""
import copy
import sys

sys.path.insert(0, __import__("os").path.dirname(__import__("os").path.abspath(__file__)))
from v4math import (  # noqa: E402
    Q96, U128, U256, MIN_TICK, MAX_TICK, MathRevert, mul_div, mul_div_up, ceil_div,
    get_sqrt_price_at_tick, get_tick_at_sqrt_price, get_amount0_delta, get_amount1_delta,
    get_liquidity_for_amounts, compute_swap_step_exact_in,
)

SPEC_VERSION = "2.36"
WAD = 10 ** 18
U32 = (1 << 32) - 1
U40 = (1 << 40) - 1
U64 = (1 << 64) - 1
V = 4                      # vesting epochs (compile-time constant)
RING = V + 1               # cohort ring slots
S_SEED = 10 ** 24
DEAD = 10 ** 15
K_MED = 9
N_SNAP = 32

# Mode and event encodings
NORMAL, BREACHED, EXITED = 0, 1, 2
EV_BREACH, EV_PLACE, EV_RETIRE = 0, 1, 2
ENTRY = ["Deposit", "Withdraw", "SetPositions", "RebalanceSwap", "Claim", "TopUpBond", "Retire",
         "ReleaseBond", "Poke", "ProcessBreach", "Seed", "Settle"]
E = {n: i for i, n in enumerate(ENTRY)}
OPS = ["Accrue", "Breach", "VestTime", "Drip", "Charge", "Place", "Retire", "Claim", "TopUp", "Release", "Settle"]


class Revert(Exception):
    """A revert of the modelled contract call, carrying the binding error name."""

    def __init__(self, err):
        super().__init__(err)
        self.err = err


def sat(x, cap):
    return 0 if x < 0 else (cap if x > cap else x)


# ================================================================ tiers
class TierInvalid(Exception):
    """A tier violates the configuration checks of the deploy script (EPOCH % GRID, GRID·blocktime, RESET_RUN, CURE)."""


class Tier:
    def __init__(self, **kw):
        self.__dict__.update(kw)
        if not (self.EPOCH % self.GRID == 0 and self.GRID * self.BLOCKTIME == self.GRID_SEC and self.RESET_RUN >= 2 and
                self.CURE_BLOCKS >= self.EDGE_DELAY + 2 * self.GRID + self.REF_WINDOW // self.BLOCKTIME + self.GRID and
                self.COOLDOWN * self.BLOCKTIME >= self.H_MARKOUT + 2 * self.BLOCKTIME):
            raise TierInvalid(self.__dict__.get("NAME"))


def _m_per_block(blocktime, m_year_wad=10 ** 16):
    return m_year_wad * blocktime // (365 * 24 * 3600)


_COMMON = dict(FEE=500, SPACING=10, FEE_TICKS=5, W_TICKS=2000, BAND_SWAP=5, REF_BAND=60,
               DELTA_MIN=WAD // 2, MAX_SWAP_ACTION=2 * 10 ** 16, MAX_SWAP_EPOCH=5 * 10 ** 16, MAX_SWAPS=20,
               RESET_RUN=2, BOND_RATIO=174 * 10 ** 13, RESERVE_RATIO=6 * 10 ** 14, L_W_TARGET=1000)
TEST = Tier(NAME="TEST", BOND_MIN=0, BLOCKTIME=12, GRID=20, GRID_SEC=240, EPOCH=120, CURE_BLOCKS=200, COOLDOWN=14,
            REF_WINDOW=600, EDGE_DELAY=20, H_MARKOUT=120, NAV_MAX=U128, m_perBlock=_m_per_block(12), **_COMMON)
DEMO = Tier(NAME="DEMO", BOND_MIN=174 * 10 ** 16, BLOCKTIME=1, GRID=300, GRID_SEC=300, EPOCH=3600, CURE_BLOCKS=1800, COOLDOWN=30,
            REF_WINDOW=300, EDGE_DELAY=300, H_MARKOUT=20, NAV_MAX=1850 * 10 ** 18, m_perBlock=_m_per_block(1), **_COMMON)
MAINNET = Tier(NAME="MAINNET", BOND_MIN=174 * 10 ** 16, BLOCKTIME=12, GRID=300, GRID_SEC=3600, EPOCH=50400, CURE_BLOCKS=7200, COOLDOWN=30,
               REF_WINDOW=600, EDGE_DELAY=1800, H_MARKOUT=120, NAV_MAX=1850 * 10 ** 18, m_perBlock=_m_per_block(12), **_COMMON)
for _t in (TEST, DEMO, MAINNET):
    _t.FORFEIT_DRIP_BLOCKS = _t.EPOCH


# ================================================================ valuation
def q(x, s):
    """Quote form: token0 amount x valued in token1 at sqrt price s (floor)."""
    if s <= U128:
        return mul_div(s * s, x, 1 << 192)
    return mul_div(mul_div(s, s, 1 << 64), x, 1 << 128)


def val(x, y, s):
    return q(x, s) + y


def position_amounts(L, lo, hi, s):
    """Appendix A at sqrt price s, with the v4 removal rounding (roundUp = false)."""
    if L == 0:
        return 0, 0
    sa, sb = get_sqrt_price_at_tick(lo), get_sqrt_price_at_tick(hi)
    if s <= sa:
        return get_amount0_delta(sa, sb, L, False), 0
    if s >= sb:
        return 0, get_amount1_delta(sa, sb, L, False)
    return get_amount0_delta(s, sb, L, False), get_amount1_delta(sa, s, L, False)


class Holdings:
    """`struct Holdings { uint128 idle0; uint128 idle1; uint128 L; int24 lo; int24 hi; }`."""
    __slots__ = ("idle0", "idle1", "L", "lo", "hi")

    def __init__(self, idle0=0, idle1=0, L=0, lo=0, hi=0):
        self.idle0, self.idle1, self.L, self.lo, self.hi = idle0, idle1, L, lo, hi

    def d(self):
        return dict(idle0=self.idle0, idle1=self.idle1, L=self.L, lo=self.lo, hi=self.hi)

    def copy(self):
        return Holdings(self.idle0, self.idle1, self.L, self.lo, self.hi)


def value(h, sqrt_p):
    """ExecLedger.value(Holdings, sqrtP) -> (nav, posValue)."""
    x, y = position_amounts(h.L, h.lo, h.hi, sqrt_p)
    return val(h.idle0 + x, h.idle1 + y, sqrt_p), val(x, y, sqrt_p)


def delta_of(h, sqrt_p):
    """δ = min(WAD, positionValue·WAD / NAV) at sqrt_p (floor; 0 when NAV = 0)."""
    nav, pv = value(h, sqrt_p)
    return 0 if nav == 0 else min(WAD, pv * WAD // nav)


# ================================================================ ExecLedger.measure
def measure_c(before, after, sqrt_s0, sqrt_spot, sqrt_ref, reserve, S, unvested, bond):
    """ExecLedger.measure (repair C) -> (charge, pStar, navBurn, burn, burnR, ok).
    loss_p = NAV_before(p) − NAV_after(p) over the legs in the order spot, s0, ref (a later leg replaces only on a
    strictly larger loss, so p* = sqrtSpot on ties, incl. charge = 0); charge = max(0, max loss);
    NAV' = navBurn = the smallest NAV_after over the three legs (v2.34, owner F9-02: no curator-chosen leg lowers the
    shares); burn = ceilDiv(charge·S, NAV'); burnR = ceilDiv((charge + reserve)·S, NAV') − burn; ok iff
    burn + burnR ≤ unvested + bond. A positive amount against NAV' = 0 saturates at type(uint256).max."""
    legs = [(value(before, p)[0] - value(after, p)[0], p, value(after, p)[0]) for p in (sqrt_spot, sqrt_s0, sqrt_ref)]
    loss, p_star, nav_after = legs[0]
    for leg in legs[1:]:
        if leg[0] > loss:
            loss, p_star, nav_after = leg
    charge = loss if loss > 0 else 0
    nav_after = min(leg[2] for leg in legs)   # the burn price: the smallest NAV_after over the legs

    def shares(x):
        if not x:
            return 0
        return ceil_div(x * S, nav_after) if nav_after else U256
    burn = shares(charge)
    tot = shares(charge + reserve)
    burn_r = tot - burn if tot != U256 else U256
    ok = burn + burn_r <= unvested + bond     # a saturated amount exceeds any cover (unvested and bond are uint128)
    return charge, p_star, nav_after, burn, burn_r, ok


def measure(before, after, sqrt_spot, sqrt_ref, S, unvested, bond):
    """The two-leg form (s0 = spot, no reserve) used by the G0 cells and the status-quo controls -> (charge, pStar,
    navBurn, burn, ok). Identical to `measure_c` with sqrtS0 = sqrtSpot and reserve 0."""
    c, p, n, b, _, ok = measure_c(before, after, sqrt_spot, sqrt_spot, sqrt_ref, 0, S, unvested, bond)
    return c, p, n, b, ok


def settle_extra(before, after, tick_mk, fee_ticks, charge0, reserve, now_h, S, reserve_shares):
    """ExecLedger.settleExtra (repair C settlement with a successful window read) -> (extra, pMk, burnS).
    Legs sqrtAt(max(MIN_TICK, tick_mk − fee_ticks)) then sqrtAt(min(MAX_TICK, tick_mk + fee_ticks + 1)) (the upper leg is
    rounded up: tryWindow returns the floor of the mean tick, owner R1-02, v2.34; the later replaces only on a strictly larger
    loss); extra = min(reserve, max(0, loss_mk − charge0)); burnS = min(reserveShares, ceilDiv(extra·S,
    NAV_now(pMk))) at the holdings now; reserveShares if NAV_now(pMk) = 0 and extra > 0; 0 if extra = 0."""
    p_lo = get_sqrt_price_at_tick(max(MIN_TICK, min(MAX_TICK, tick_mk - fee_ticks)))
    p_hi = get_sqrt_price_at_tick(max(MIN_TICK, min(MAX_TICK, tick_mk + fee_ticks + 1)))
    l_lo = value(before, p_lo)[0] - value(after, p_lo)[0]
    l_hi = value(before, p_hi)[0] - value(after, p_hi)[0]
    loss_mk, p_mk = (l_hi, p_hi) if l_hi > l_lo else (l_lo, p_lo)
    extra = min(reserve, max(0, loss_mk - charge0))
    if not extra:
        return 0, p_mk, 0
    nav_now = value(now_h, p_mk)[0]
    if not nav_now:
        return extra, p_mk, reserve_shares
    return extra, p_mk, min(reserve_shares, ceil_div(extra * S, nav_now))


# ================================================================ Placement
def placement_check(lo, hi, tick_spot, ref_tick, band, w_ticks, spacing):
    """First failing code in numeric order: 1 fence, 2 width, 3 alignment, 4 spot not contained,
    5 reference not contained; 0 ok."""
    if abs(tick_spot - ref_tick) > band:
        return 1
    if hi - lo != w_ticks:
        return 2
    if lo % spacing != 0 or hi % spacing != 0:
        return 3
    if not (lo <= tick_spot < hi):
        return 4
    if not (lo <= ref_tick < hi):
        return 5
    return 0


PLACEMENT_ERR = {1: "Fence", 2: "Width", 3: "Alignment", 4: "NotContained", 5: "NotContained"}


def _liq0_sat(sa, sb, amount0):
    """LiquidityAmounts.getLiquidityForAmount0 with the result saturated at type(uint128).max instead of
    SafeCast reverting: next to an edge the non-binding leg overflows."""
    if sa > sb:
        sa, sb = sb, sa
    inter = mul_div(sa, sb, Q96)
    return min(amount0 * inter // (sb - sa), U128)


def _liq1_sat(sa, sb, amount1):
    if sa > sb:
        sa, sb = sb, sa
    return min(amount1 * Q96 // (sb - sa), U128)


def liquidity_for_amounts_sat(s, sa, sb, amount0, amount1):
    """getLiquidityForAmounts with saturating legs (the min of the two legs never overflows the binding one)."""
    if s <= sa:
        return _liq0_sat(sa, sb, amount0)
    if s < sb:
        return min(_liq0_sat(s, sb, amount0), _liq1_sat(sa, s, amount1))
    return _liq1_sat(sa, sb, amount1)


def placement_size(idle, lo, hi, sqrt_spot, sqrt_ref):
    """-> (L, after, deltaAfter). L = getLiquidityForAmounts(spot, lo, hi, idle0, idle1) with each leg
    saturated at type(uint128).max, minus a headroom of L/1e9 + 1 (floored at 0); the amounts
    consumed are the v4 add amounts at spot (round up); deltaAfter = δ of the resulting holdings at sqrtRef."""
    sa, sb = get_sqrt_price_at_tick(lo), get_sqrt_price_at_tick(hi)
    L0 = liquidity_for_amounts_sat(sqrt_spot, sa, sb, idle.idle0, idle.idle1)
    head = L0 // 10 ** 9 + 1
    L = L0 - head if L0 > head else 0
    if L == 0:
        a0 = a1 = 0
    elif sqrt_spot <= sa:
        a0, a1 = get_amount0_delta(sa, sb, L, True), 0
    elif sqrt_spot >= sb:
        a0, a1 = 0, get_amount1_delta(sa, sb, L, True)
    else:
        a0, a1 = get_amount0_delta(sqrt_spot, sb, L, True), get_amount1_delta(sa, sqrt_spot, L, True)
    if a0 > idle.idle0 or a1 > idle.idle1:
        raise MathRevert("InsufficientIdle")  # unreachable with the headroom; a settle would fail
    after = Holdings(idle.idle0 - a0, idle.idle1 - a1, L, lo, hi)
    return L, after, min(delta_of(after, sqrt_ref), U64)


# ================================================================ Ladder
def ladder_next(mode, breaches, ev, count):
    """-> (mode, breaches, exited). `exited` is true only on a transition from NORMAL/BREACHED to EXITED."""
    if ev == EV_BREACH:
        if count == 0:
            return mode, breaches, False
        b = sat(breaches + count, U32)
        if mode == EXITED:
            return EXITED, b, False
        if b >= 3:
            return EXITED, b, True
        return BREACHED, b, False
    if ev == EV_PLACE:
        if mode == EXITED:
            raise Revert("BadMode")
        return NORMAL, breaches, False
    if ev == EV_RETIRE:
        if mode == EXITED:
            raise Revert("BadMode")
        return EXITED, breaches, True
    raise ValueError(ev)


_ALLOWED = {  # entry-point table; Seed is gated by onlyFactory/once and never by this table
    "Deposit": (1, 1, 0), "Withdraw": (1, 1, 1), "SetPositions": (1, 1, 0), "RebalanceSwap": (1, 1, 0),
    "Claim": (1, 1, 1), "TopUpBond": (1, 1, 0), "Retire": (1, 1, 0), "ReleaseBond": (0, 0, 1),
    "Poke": (1, 1, 1), "ProcessBreach": (1, 1, 1), "Seed": (0, 0, 0), "Settle": (1, 1, 1),
}


def ladder_allowed(mode, entry):
    return bool(_ALLOWED[ENTRY[entry]][mode])


# ================================================================ FeeBank
class Bank:
    FIELDS = ("maturedPending", "vestedUnclaimed", "bondShares", "forfeitPool", "forfeitTotal", "forfeitStart",
              "dripBlocks", "releaseDelay", "breaches", "mode", "placedByCurator", "exitBlock",
              "reserveShares", "reserveBond", "reserveForfeit")

    def __init__(self):
        self.cohorts = [[0, 0] for _ in range(RING)]  # [epoch, shares] keyed by epoch mod (V+1)
        self.maturedPending = self.vestedUnclaimed = self.bondShares = 0
        self.forfeitPool = self.forfeitTotal = self.forfeitStart = 0
        self.dripBlocks = self.releaseDelay = 0
        self.breaches = 0
        self.mode = NORMAL
        self.placedByCurator = False
        self.exitBlock = 0
        self.reserveShares = self.reserveBond = 0   # repair C: the encumbered reserve (outside every bucket)
        self.reserveForfeit = False

    def d(self):
        out = {"cohorts": [{"epoch": e, "shares": s} for e, s in self.cohorts]}
        for f in self.FIELDS:
            out[f] = getattr(self, f)
        return out

    def cohort_total(self):
        return sum(s for _, s in self.cohorts)

    def unvested(self):
        """unvested := unvested cohorts + maturedPending."""
        return self.cohort_total() + self.maturedPending


def _drip_now(b, now):
    """Burn the forfeit drip owed at `now` (cumulative target). Returns shares burned."""
    T = b.forfeitTotal
    if T == 0 or b.forfeitPool == 0:
        return 0
    if b.dripBlocks == 0:
        so_far = T
    else:
        el = now - b.forfeitStart if now > b.forfeitStart else 0
        so_far = min(T, T * el // b.dripBlocks)
    burn = so_far - (T - b.forfeitPool)
    burn = sat(burn, b.forfeitPool)
    b.forfeitPool -= burn
    return burn


def _forfeit_all(b, now):
    """Move every unvested cohort and maturedPending to forfeitPool; restart the drip only if the new forfeit
    is > 0 (the drip owed so far must already have been burned)."""
    new = b.cohort_total() + b.maturedPending
    for c in b.cohorts:
        c[1] = 0  # an emptied slot keeps its epoch; only shares are cleared
    b.maturedPending = 0
    if new > 0:
        b.forfeitPool = sat(b.forfeitPool + new, U128)
        b.forfeitTotal = b.forfeitPool
        b.forfeitStart = now
    return new


def _take(b, need):
    """Take `need` shares youngest cohort first, then maturedPending, then the bond. Returns (from bank, from bond)."""
    bank = 0
    for c in sorted(b.cohorts, key=lambda c: -c[0]):  # youngest first
        if need == 0:
            break
        x = min(need, c[1])
        c[1] -= x
        need -= x
        bank += x
    x = min(need, b.maturedPending)
    b.maturedPending -= x
    need -= x
    bank += x
    b.bondShares -= need
    return bank, need


def fee_bank_apply(b, op, args):
    """FeeBank.apply(Bank storage, Op, args) -> (burnShares, payShares, a, b2). Mutates `b`; raises Revert.
    Per-op return slots: Charge → (bank burned, bond burned); Breach/Retire → (forfeited, mode after);
    VestTime/Accrue → (vested, moved to maturedPending); Drip → (burned, 0); Claim/TopUp/Release/Place →
    (shares moved, 0)."""
    if op == "Accrue":
        e, shares, gate = args["epoch"], args["shares"], args["gateOpen"]
        if shares == 0:
            return 0, 0, 0, 0
        slot = b.cohorts[e % RING]
        vested = moved = 0
        if slot[1] > 0 and slot[0] != e:
            if gate:
                b.vestedUnclaimed = sat(b.vestedUnclaimed + slot[1], U128)
                vested = slot[1]
            else:
                b.maturedPending = sat(b.maturedPending + slot[1], U128)
                moved = slot[1]
            slot[0], slot[1] = e, 0
        slot[0] = e
        slot[1] = sat(slot[1] + shares, U128)
        return 0, 0, vested, moved
    if op == "Breach":
        count, now = args["count"], args["now"]
        burned = _drip_now(b, now)
        forfeited = _forfeit_all(b, now)
        if b.reserveShares:
            b.reserveForfeit = True
        mode, br, exited = ladder_next(b.mode, b.breaches, EV_BREACH, count)
        b.mode, b.breaches = mode, br
        if exited:
            b.exitBlock = now
            b.placedByCurator = False
        return burned, 0, forfeited, b.mode
    if op == "VestTime":
        ne, gate = args["nowEpoch"], args["gateOpen"]
        vested = moved = 0
        for c in b.cohorts:
            if c[1] > 0 and ne >= c[0] + 1 + V:
                if gate:
                    vested += c[1]
                else:
                    moved += c[1]
                c[1] = 0
        if gate:
            vested += b.maturedPending
            b.maturedPending = 0
            b.vestedUnclaimed = sat(b.vestedUnclaimed + vested, U128)
        else:
            b.maturedPending = sat(b.maturedPending + moved, U128)
        return 0, 0, vested, moved
    if op == "Drip":
        burned = _drip_now(b, args["now"])
        return burned, 0, burned, 0
    if op == "Charge":
        burn, burn_r = args["burn"], args.get("burnR", 0)
        if burn + burn_r > b.unvested() + b.bondShares:
            raise Revert("Insolvent")
        bank_burn, bond_burn = _take(b, burn)
        if burn_r:
            if b.reserveShares:
                raise Revert("MarkoutPending")   # at most one reserve (unreachable through the vault: COOLDOWN)
            _, from_bond = _take(b, burn_r)
            b.reserveShares, b.reserveBond, b.reserveForfeit = burn_r, from_bond, False
        return burn, 0, bank_burn, bond_burn
    if op == "Settle":
        burn_s, e, now = args["burnS"], args["epoch"], args["now"]
        x = min(burn_s, b.reserveShares)
        nonbond = b.reserveShares - b.reserveBond
        from_nb = min(x, nonbond)
        rel_bond = b.reserveBond - (x - from_nb)
        rel_nb = nonbond - from_nb
        b.bondShares = sat(b.bondShares + rel_bond, U128)
        dripped = 0
        if rel_nb:
            if b.reserveForfeit:
                dripped = _drip_now(b, now)
                b.forfeitPool = sat(b.forfeitPool + rel_nb, U128)
                b.forfeitTotal = b.forfeitPool
                b.forfeitStart = now
            else:
                slot = b.cohorts[e % RING]
                if slot[1] > 0 and slot[0] != e:
                    b.maturedPending = sat(b.maturedPending + slot[1], U128)
                    slot[1] = 0
                slot[0] = e
                slot[1] = sat(slot[1] + rel_nb, U128)
        b.reserveShares = b.reserveBond = 0
        b.reserveForfeit = False
        return x + dripped, 0, rel_bond, rel_nb
    if op == "Place":
        mode, br, _ = ladder_next(b.mode, b.breaches, EV_PLACE, 0)
        b.mode, b.breaches = mode, br
        b.placedByCurator = True
        return 0, 0, 0, 0
    if op == "Retire":
        now = args["now"]
        mode, br, exited = ladder_next(b.mode, b.breaches, EV_RETIRE, 0)  # reverts BadMode in EXITED
        burned = _drip_now(b, now)
        forfeited = _forfeit_all(b, now)
        if b.reserveShares:
            b.reserveForfeit = True
        b.mode, b.breaches = mode, br
        if exited:
            b.exitBlock = now
            b.placedByCurator = False
        return burned, 0, forfeited, b.mode
    if op == "Claim":
        x = b.vestedUnclaimed
        b.vestedUnclaimed = 0
        return 0, x, x, 0
    if op == "TopUp":
        b.bondShares = sat(b.bondShares + args["shares"], U128)
        return 0, 0, args["shares"], 0
    if op == "Release":
        shares, now = args["shares"], args["now"]
        if b.mode != EXITED:
            raise Revert("BadMode")
        if now < b.exitBlock + b.releaseDelay:
            raise Revert("BondLocked")
        x = min(shares, b.bondShares)
        b.bondShares -= x
        return 0, x, x, 0
    raise ValueError(op)


# ================================================================ reference source (test double)
class RefSource:
    """The reference pool's 10-minute TWAP as a function of time: `tick_at(ts)` gives the TWAP ending at ts.
    `fail` is a set of end-timestamps (or a callable) for which `observe` fails."""

    def __init__(self, tick_at, fail=None, window=None, fail_window=None):
        self.tick_at = tick_at
        self.fail = fail if fail is not None else set()
        self.window = window            # (start, end) -> TWAP tick over (start, end]; default: tick_at(end)
        self.fail_window = fail_window  # (start, end) -> True when observe fails (e.g. older than the history)

    def read(self, ts, seconds_ago):
        if seconds_ago == 0:
            self.live_count = getattr(self, "live_count", 0) + 1  # every live read (tryRef or liveRef)
        end = ts - seconds_ago
        failed = self.fail(end) if callable(self.fail) else end in self.fail
        if failed:
            return False, 0
        return True, max(MIN_TICK, min(MAX_TICK, self.tick_at(end)))

    def read_window(self, ts, a, b):
        """tryWindow(a, b) at time ts: the TWAP over (ts − a, ts − b], floor; fail-soft."""
        start, end = ts - a, ts - b
        if self.fail_window is not None and self.fail_window(start, end):
            return False, 0
        t = self.window(start, end) if self.window is not None else self.tick_at(end)
        if t is None:
            return False, 0
        return True, max(MIN_TICK, min(MAX_TICK, t))


def in_rng(lo, hi, t):
    return lo <= t < hi


# ================================================================ hook
class Hook:
    def __init__(self, tier, ref):
        self.t, self.ref = tier, ref
        self.armed = False
        self.lastSnap = 0
        self.tauV = self.tauB = self.outBlocks = 0
        self.refTick = 0
        self.refGrid = 0
        self.refCachedAt = 0
        self.resetStreak = 0
        self.gapSinceVerify = False
        self.emptyRange = True
        self.vaultLo = self.vaultHi = 0
        self.breachSeq = self.pendingBreaches = 0
        self.lastPendingBlock = 0
        self.tauVAtLastPending = 0
        self.epochStartRef = 0
        self.epochStartRefEpoch = None
        self.witLo, self.witHi = 0, 0
        self.ring = []          # (block, tick), newest last, at most N_SNAP
        self.med9 = 0
        self.frozen = {}
        self.events = []
        self.ts = {}            # block -> timestamp seen

    # ---- views
    def fresh(self, now):
        return now < self.refCachedAt + 2 * self.t.GRID

    def obs(self, now, tick_spot):
        return dict(med9=self.med9, tickSpot=tick_spot, refTick=self.refTick, refCachedAt=self.refCachedAt,
                    refFresh=self.fresh(now), resetStreak=self.resetStreak, gapSinceVerify=self.gapSinceVerify,
                    emptyRange=self.emptyRange, tauV=self.tauV, tauB=self.tauB, outBlocks=self.outBlocks,
                    breachSeq=self.breachSeq, pendingBreaches=self.pendingBreaches,
                    lastPendingBlock=self.lastPendingBlock, tauVAtLastPending=self.tauVAtLastPending,
                    gridIndex=now // self.t.GRID, epoch=now // self.t.EPOCH,
                    vaultLo=self.vaultLo, vaultHi=self.vaultHi)

    def try_ref(self, ts):
        return self.ref.read(ts, 0)

    def try_window(self, ts, a, b):
        return self.ref.read_window(ts, a, b)

    def live_ref(self, ts):
        ok, t = self.ref.read(ts, 0)
        if not ok:
            raise Revert("RefUnreadable")
        return t

    # ---- internals
    def _write_cache(self, t, k, now):
        self.refTick, self.refGrid, self.refCachedAt = t, k, now
        e = now // self.t.EPOCH
        if self.epochStartRefEpoch != e:
            self.epochStartRef, self.epochStartRefEpoch = t, e

    def _commit(self, ok, t, k, now):
        """Step 5."""
        if not ok:
            self.events.append(("RefReadFailed", k))
            return
        lo, hi = self.vaultLo, self.vaultHi
        if in_rng(lo, hi, t):
            self.resetStreak = min(self.resetStreak + 1, self.t.RESET_RUN) if self.refGrid + 1 == k else 1
        else:
            self.resetStreak = 0
        self._write_cache(t, k, now)
        self.events.append(("RefRefreshed", k, t))
        if self.resetStreak >= self.t.RESET_RUN:
            self.outBlocks = 0
            self.gapSinceVerify = False
            self.events.append(("GapCleared", 1))
            self.resetStreak = 0

    def _latch(self, old, new):
        """Step 6: one breach per multiple of CURE_BLOCKS crossed in step 3 (at most 3 per call)."""
        C = self.t.CURE_BLOCKS
        j = old // C + 1
        n = 0
        while j * C <= new and n < 3:
            self.breachSeq = sat(self.breachSeq + 1, U32)
            self.pendingBreaches = sat(self.pendingBreaches + 1, U32)
            self.lastPendingBlock = self.lastSnap + (j * C - old)
            self.tauVAtLastPending = self.tauV
            self.events.append(("BreachLatched", self.breachSeq, self.lastPendingBlock))
            j += 1
            n += 1

    def _insert(self, now, tick):
        self.ring.append((now, tick))
        if len(self.ring) > N_SNAP:
            self.ring.pop(0)
        last = sorted(t for _, t in self.ring[-K_MED:])
        if last:
            self.med9 = last[(len(last) - 1) // 2]

    def snapshot(self, now, ts, sqrt_price, tick):
        """snapshotIfNewBlock(): the snapshot procedure (steps 1-6) on the first touch of block `now`."""
        if now == self.lastSnap and self.ring:
            return self.obs(now, tick)
        self.ts[now] = ts
        if not self.armed:
            # A snapshot before arming credits nothing: it records the entry only.
            self._insert(now, tick)
            self.lastSnap = now
            return self.obs(now, tick)
        G = self.t.GRID
        # 1. read
        k = ts // self.t.GRID_SEC
        rd = self.ref.read(ts, ts - k * self.t.GRID_SEC) if k > self.refGrid else None
        # 2-3. close the span [lastSnap, now) at the block-start price of `now`
        lo, hi = self.vaultLo, self.vaultHi
        last = self.lastSnap
        trunc = max(0, min(now, self.refCachedAt + 2 * G) - last)
        old_out = self.outBlocks
        tauv0, taub0 = self.tauV, self.tauB
        credit_v = (not self.emptyRange and get_sqrt_price_at_tick(lo) < sqrt_price < get_sqrt_price_at_tick(hi)
                    and in_rng(lo, hi, self.refTick))
        if credit_v:
            self.tauV = sat(self.tauV + trunc, U64)
        credit_b = in_rng(self.witLo, self.witHi, tick)
        if credit_b:
            self.tauB = sat(self.tauB + trunc, U64)
        if not in_rng(lo, hi, self.refTick):
            self.outBlocks = sat(self.outBlocks + trunc, U64)
        new_out = self.outBlocks
        if now > self.refCachedAt + 2 * G:
            self.gapSinceVerify = True
            self.events.append(("GapSet", now))
        # freeze at most the last 8 grid boundaries B with last < B <= now (records only)
        bmax = (now // G) * G
        bmin = max(last // G * G + G, bmax - 7 * G)
        for B in range(bmin, bmax + 1, G):
            if B <= last:
                continue
            cr = max(0, min(min(B, self.refCachedAt + 2 * G) - last, trunc))
            self.frozen[B // G] = (self.med9, tauv0 + (cr if credit_v else 0), taub0 + (cr if credit_b else 0))
        # 4. insert
        self._insert(now, tick)
        # 5. commit
        if rd is not None:
            self._commit(rd[0], rd[1], k, now)
        # 6. latch on the step-3 crossing
        self._latch(old_out, new_out)
        self.lastSnap = now
        return self.obs(now, tick)

    def refresh(self, now, ts):
        """_refresh(): steps 1 and 5 only."""
        k = ts // self.t.GRID_SEC
        if k > self.refGrid:
            ok, t = self.ref.read(ts, ts - k * self.t.GRID_SEC)
            self._commit(ok, t, k, now)

    def refresh_ref(self, now, ts, sqrt_price, tick):
        self.snapshot(now, ts, sqrt_price, tick)
        self.refresh(now, ts)

    def arm_at_seed(self, now, ts):
        self.armed = True
        self.lastSnap = now
        self.refresh(now, ts)
        if self.refCachedAt != now:
            raise Revert("RefUnreadable")

    def set_vault_range(self, lo, hi, live_tick, now, ts):
        self._latch(self.outBlocks, self.outBlocks)  # defensive step 6 (no crossing left after the prologue)
        self.vaultLo, self.vaultHi = lo, hi
        self.emptyRange = False
        self._write_cache(live_tick, ts // self.t.GRID_SEC, now)
        self.gapSinceVerify = False
        self.events.append(("GapCleared", 0))
        self.resetStreak = 0
        self.outBlocks = 0
        self.events.append(("RangeRegistered", lo, hi, live_tick))

    def ack_breaches(self, n):
        self.pendingBreaches = sat(self.pendingBreaches - n, U32)


# ================================================================ pool simulator (for the gate cells)
class Pool:
    """A single concentrated-liquidity pool with v4 swap-step math, tick crossing and fee growth, used by the
    gate cells and the self-check to produce real holdings before and after an action. It is a test double,
    not part of the vectors (action vectors take holdings as inputs)."""

    def __init__(self, sqrt_price, fee, spacing=10):
        self.s = sqrt_price
        self.spacing = spacing
        self.tick = get_tick_at_sqrt_price(sqrt_price)
        self.fee = fee
        self.L = 0
        self.net = {}        # tick -> liquidityNet
        self.gross = {}
        self.fg = [0, 0]     # fee growth global, Q128
        self.out = {}        # tick -> [fgOutside0, fgOutside1]
        self.pos = {}        # (key, lo, hi) -> [L, fgIn0Last, fgIn1Last]

    def _fg_inside(self, lo, hi):
        o_lo = self.out.get(lo, [0, 0])
        o_hi = self.out.get(hi, [0, 0])
        r = []
        for i in (0, 1):
            below = o_lo[i] if self.tick >= lo else self.fg[i] - o_lo[i]
            above = o_hi[i] if self.tick < hi else self.fg[i] - o_hi[i]
            r.append((self.fg[i] - below - above) % (1 << 256))
        return r

    def _update_tick(self, t, dL, upper):
        if t not in self.gross:
            self.gross[t] = 0
            self.net[t] = 0
            self.out[t] = list(self.fg) if self.tick >= t else [0, 0]
        self.gross[t] += dL
        self.net[t] += -dL if upper else dL
        if self.gross[t] == 0:
            del self.gross[t], self.net[t], self.out[t]

    def collect(self, key, lo, hi):
        """Fees owed to the position (key, lo, hi); v4 pays all of them on any modify."""
        p = self.pos.get((key, lo, hi))
        if not p or p[0] == 0:
            return 0, 0
        fi = self._fg_inside(lo, hi)
        f0 = mul_div((fi[0] - p[1]) % (1 << 256), p[0], 1 << 128)
        f1 = mul_div((fi[1] - p[2]) % (1 << 256), p[0], 1 << 128)
        p[1], p[2] = fi
        return f0, f1

    def modify(self, key, lo, hi, dL):
        """Add (dL > 0) or remove (dL < 0) liquidity of position (key, lo, hi). Returns (amount0, amount1,
        fees0, fees1): the signed amounts the caller pays (+) or receives (-), plus the fees collected."""
        pk = (key, lo, hi)
        if dL == 0 and pk not in self.pos:
            raise MathRevert("CannotUpdateEmptyPosition")  # v4: a zero-delta modify reverts on an empty position
        if self.pos.get(pk, [0])[0] + dL < 0:
            raise MathRevert("SafeCastOverflow")  # v4: removing more liquidity than the position holds reverts
        f0, f1 = self.collect(key, lo, hi)
        if dL == 0:
            return 0, 0, f0, f1
        p = self.pos.setdefault(pk, [0] + self._fg_inside(lo, hi))
        self._update_tick(lo, dL, False)
        self._update_tick(hi, dL, True)
        p[0] += dL
        if p[0] == 0:
            del self.pos[pk]
        else:
            p[1], p[2] = self._fg_inside(lo, hi)
        up = dL > 0
        a = abs(dL)
        sa, sb = get_sqrt_price_at_tick(lo), get_sqrt_price_at_tick(hi)
        if self.tick < lo:
            a0, a1 = get_amount0_delta(sa, sb, a, up), 0
        elif self.tick < hi:
            a0, a1 = get_amount0_delta(self.s, sb, a, up), get_amount1_delta(sa, self.s, a, up)
            self.L += dL
        else:
            a0, a1 = 0, get_amount1_delta(sa, sb, a, up)
        sg = 1 if up else -1
        return sg * a0, sg * a1, f0, f1

    def _next_tick(self, down):
        """TickBitmap.nextInitializedTickWithinOneWord: the next initialized tick within the current 256-spacing
        bitmap word, else the word's boundary (v4 stops a swap step there too; each step rounds separately)."""
        sp = self.spacing
        comp = self.tick // sp
        if down:
            lo_c = (comp >> 8) << 8
            ks = [t for t in self.net if lo_c * sp <= t <= comp * sp]
            nt = max(ks) if ks else lo_c * sp
        else:
            c1 = comp + 1
            hi_c = (((c1 >> 8) + 1) << 8) - 1
            ks = [t for t in self.net if c1 * sp <= t <= hi_c * sp]
            nt = min(ks) if ks else hi_c * sp
        return max(MIN_TICK, min(MAX_TICK, nt))

    def swap(self, zero_for_one, amount_in, sqrt_limit):
        """Exact-input swap. Returns (amountIn used, amountOut)."""
        rem = amount_in
        out = 0
        guard = 0
        while rem > 0 and self.s != sqrt_limit:
            guard += 1
            assert guard < 10000, "swap made no progress"
            nt = self._next_tick(zero_for_one)
            s_next_tick = get_sqrt_price_at_tick(nt)
            target = max(s_next_tick, sqrt_limit) if zero_for_one else min(s_next_tick, sqrt_limit)
            if self.L == 0:
                s_new, ain, aout, fee = target, 0, 0, 0
            else:
                s_new, ain, aout, fee = compute_swap_step_exact_in(self.s, target, self.L, rem, self.fee)
            rem -= ain + fee
            out += aout
            if fee and self.L:
                self.fg[0 if zero_for_one else 1] += mul_div(fee, 1 << 128, self.L)
            self.s = s_new
            if s_new == s_next_tick:   # v4: at a step's next tick (initialized or a word boundary) tick = nt − 1 / nt
                if nt in self.net:
                    o = self.out[nt]
                    o[0], o[1] = self.fg[0] - o[0], self.fg[1] - o[1]
                    self.L += -self.net[nt] if zero_for_one else self.net[nt]
                self.tick = nt - 1 if zero_for_one else nt
            else:
                self.tick = get_tick_at_sqrt_price(s_new)
            if self.L == 0 and s_new == target and target == sqrt_limit:
                break
        return amount_in - rem, out

    def push_to(self, sqrt_target):
        """Move the price to sqrt_target with an exact-input swap of unbounded size. Returns (in, out, zfo)."""
        zfo = sqrt_target < self.s
        ain, aout = self.swap(zfo, 1 << 200, sqrt_target)
        return ain, aout, zfo


# ================================================================ vault
class Vault:
    """The vault with its hook and pool. Curator actions execute against `Pool`; holdings are tracked in
    storage (idle as internal balances). Every call is atomic: a Revert restores the prior state."""

    KEY = "vault"

    def __init__(self, tier, ref, pool, sqrt_ref_price=None):
        self.t = tier
        self.hook = Hook(tier, ref)
        self.pool = pool
        self.h = Holdings()
        self.S = 0
        self.NAV0 = 0
        self.delta0 = 0
        self.tauV0 = 0
        self.segStart = 0
        self.epoch = 0
        self.epochSwapFrac = 0
        self.swapCount = 0
        self.lastAction = 0
        self.baseOwed = 0
        self.bank = Bank()
        self.records = {}
        self.events = []
        self.REF_SQRT_PRICE = sqrt_ref_price
        self.live_reads = 0
        self.mint_reads = 0
        self.pending = None   # repair C: {tAction, before, after, charge0, reserve}

    # ---- atomic call wrapper
    def call(self, fn, *a, **kw):
        snap = copy.deepcopy(self.__dict__, {id(self.hook.ref): self.hook.ref})  # the reference source is external
        try:
            return fn(*a, **kw)
        except (Revert, MathRevert):
            self.__dict__.clear()
            self.__dict__.update(snap)
            raise

    def nav(self, sqrt_p):
        return value(self.h, sqrt_p)[0]

    def rec(self, e):
        return self.records.setdefault(e, dict(base=0, charge=0, minted=0, bankBurned=0, bondBurned=0,
                                               forfeited=0, forfeitValue=0, flags=set(), tauV=0))

    # ---- prologue pieces
    def _snapshot(self, now, ts):
        return self.hook.snapshot(now, ts, self.pool.s, self.pool.tick)

    def _collect_first(self):
        if self.h.L > 0:
            _, _, f0, f1 = self.pool.modify(self.KEY, self.h.lo, self.h.hi, 0)
            self.h.idle0 += f0
            self.h.idle1 += f1

    def _base_amount(self, dtau):
        if self.NAV0 == 0 or dtau <= 0:
            return 0
        return mul_div(mul_div(self.t.m_perBlock, self.NAV0, WAD), self.delta0 * dtau, WAD)

    def _eligible(self):
        return self.bank.placedByCurator and self.bank.mode in (NORMAL, BREACHED)

    def _checkpoint(self, o, now, ts):
        """The breach-first checkpoint. Returns (liveRead, liveTick)."""
        if self.S == 0:
            return False, 0
        b = self.bank
        ne = now // self.t.EPOCH
        lo, hi = self.hook.vaultLo, self.hook.vaultHi
        # (1) gate
        live_read, live_tick = False, 0
        gate = o["refFresh"] and not o["emptyRange"] and not o["gapSinceVerify"] and in_rng(lo, hi, o["refTick"])
        vestable = b.maturedPending > 0 or any(s > 0 and ne >= e + 1 + V for e, s in b.cohorts)
        gate_open = False
        if gate and vestable:
            ok, t = self.hook.try_ref(ts)
            self.live_reads += 1
            live_read, live_tick = ok, t
            gate_open = ok and in_rng(lo, hi, t)
        r = self.rec(ne)
        if o["gapSinceVerify"]:
            r["flags"].add("unverified")
        # (2) drip
        burned, _, _, _ = fee_bank_apply(b, "Drip", dict(now=now))
        self.S -= burned
        # (3) pending breaches
        if o["pendingBreaches"] > 0:
            n = o["pendingBreaches"]
            if self._eligible():
                fv = self._base_amount(o["tauVAtLastPending"] - self.tauV0)
                r["forfeitValue"] += fv
            r["forfeitValue"] += self.baseOwed
            self.baseOwed = 0
            burned, _, forfeited, _ = fee_bank_apply(b, "Breach", dict(count=n, now=now))
            self.S -= burned
            r["forfeited"] += forfeited
            r["flags"].add("breach")
            self.hook.ack_breaches(n)
            self.tauV0 = o["tauVAtLastPending"]
            self.segStart = o["lastPendingBlock"]
            self.events.append(("BreachProcessed", n, o["lastPendingBlock"], forfeited))
        # (4) accrue
        base = self._base_amount(o["tauV"] - self.tauV0) if self._eligible() else 0
        amount = base + self.baseOwed
        r["base"] += base
        if amount > 0:
            price = None
            if o["refFresh"]:
                price = o["refTick"]
            else:
                ok, t = self.hook.try_ref(ts)
                self.mint_reads += 1
                price = t if ok else None
            minted = False
            if price is not None:
                navp = self.nav(get_sqrt_price_at_tick(price))
                if navp > amount:
                    shares = mul_div(amount, self.S, navp - amount)
                    fee_bank_apply(b, "Accrue", dict(epoch=ne, shares=shares, gateOpen=gate_open))
                    self.S += shares
                    r["minted"] += shares
                    self.baseOwed = 0
                    minted = True
            if not minted:
                self.baseOwed = amount
                r["flags"].add("deferred")
                self.events.append(("CheckpointDeferred",))
        self.tauV0 = o["tauV"]
        self.segStart = now
        self.events.append(("Checkpoint", o["tauV"], o["tauB"], self.NAV0, self.delta0, base, self.h.L, lo, hi))
        # (5) vest
        fee_bank_apply(b, "VestTime", dict(nowEpoch=ne, gateOpen=gate_open))
        # (6) epoch roll
        if ne > self.epoch:
            self.rec(self.epoch)["end"] = now
            if ne > self.epoch + 1:
                self.rec(self.epoch + 1)["flags"].add("stale")
            self.epoch = ne
            self.epochSwapFrac = 0
            self.swapCount = 0
        return live_read, live_tick

    def _settle(self, now, ts):
        """Prologue step 0 (repair C): settle a due pending action against the markout window. Never reverts."""
        pd = self.pending
        t = self.t
        if pd is None:
            return None
        if ts < pd["tAction"] + 2 * t.BLOCKTIME + t.H_MARKOUT:
            return None
        a = ts - pd["tAction"] - t.BLOCKTIME
        ok, tick = self.hook.try_window(ts, a, a - t.H_MARKOUT)
        b = self.bank
        if ok:
            extra, p_mk, burn_s = settle_extra(pd["before"], pd["after"], tick, t.FEE_TICKS, pd["charge0"], pd["reserve"],
                                               self.h, self.S, b.reserveShares)
        else:
            extra, burn_s = pd["reserve"], b.reserveShares
        burned, _, rel_bond, rel_nb = fee_bank_apply(b, "Settle", dict(burnS=burn_s, epoch=now // t.EPOCH, now=now))
        self.S -= burned
        self.rec(now // t.EPOCH)["charge"] += extra
        self.pending = None
        self.events.append(("Settled", pd["charge0"], extra, tick, not ok))   # a failed read returns tick 0
        return extra, burn_s, ok

    def prologue(self, now, ts):
        self._settle(now, ts)
        o = self._snapshot(now, ts)
        self._collect_first()
        return o, self._checkpoint(o, now, ts)

    def _gate(self, entry):
        if not ladder_allowed(self.bank.mode, E[entry]):
            raise Revert("BadMode")

    # ---- flows
    def seed(self, a0, a1, now, ts):
        if a0 + a1 == 0:
            raise Revert("ZeroShares")  # v2.32 §3.2: a valueless seed
        self.h = Holdings(a0, a1)
        self._snapshot(now, ts)
        self.S = S_SEED
        self.bank.bondShares = S_SEED - DEAD
        self.hook.arm_at_seed(now, ts)
        if self.REF_SQRT_PRICE is not None:
            nav_ref = self.nav(self.REF_SQRT_PRICE)
            if mul_div(nav_ref, self.bank.bondShares, self.S) < self.t.BOND_MIN:
                raise Revert("BondMin")
        self.epoch = now // self.t.EPOCH
        self.NAV0 = self.nav(get_sqrt_price_at_tick(self.hook.refTick))
        self.delta0 = 0
        self.tauV0 = self.hook.tauV
        self.segStart = now
        self.bank.mode = NORMAL
        self.bank.dripBlocks = self.t.FORFEIT_DRIP_BLOCKS
        self.bank.releaseDelay = V * self.t.EPOCH

    def _rebase_flow(self, S_before):
        if S_before > 0:
            self.NAV0 = mul_div(self.NAV0, self.S, S_before)

    def deposit(self, shares, now, ts):
        def f():
            if shares == 0:
                raise Revert("ZeroShares")  # v2.32 §3.2: before the prologue
            self.prologue(now, ts)
            self._gate("Deposit")
            if self.pending is not None:
                raise Revert("MarkoutPending")
            S = self.S
            d0, d1, dL = ceil_div(self.h.idle0 * shares, S), ceil_div(self.h.idle1 * shares, S), ceil_div(self.h.L * shares, S)
            p0 = p1 = 0
            if self.h.L > 0 and dL > 0:
                p0, p1, _, _ = self.pool.modify(self.KEY, self.h.lo, self.h.hi, dL)
                self.h.L += dL
            self.h.idle0 += d0
            self.h.idle1 += d1
            self.S += shares
            if self.S * self.t.BOND_RATIO > self.bank.bondShares * WAD:
                raise Revert("NavCap")
            self._rebase_flow(S)
            if self.NAV0 > self.t.NAV_MAX:
                raise Revert("NavCap")
            return d0 + p0, d1 + p1
        return self.call(f)

    def withdraw(self, shares, now, ts):
        def f():
            if shares == 0:
                raise Revert("ZeroShares")  # v2.32 §3.2: before the prologue
            self.prologue(now, ts)
            self._gate("Withdraw")
            S = self.S
            o0, o1, dL = self.h.idle0 * shares // S, self.h.idle1 * shares // S, self.h.L * shares // S
            self.S -= shares
            r0 = r1 = 0
            if dL > 0:
                a0, a1, _, _ = self.pool.modify(self.KEY, self.h.lo, self.h.hi, -dL)
                self.h.L -= dL
                r0, r1 = -a0, -a1
            self.h.idle0 -= o0
            self.h.idle1 -= o1
            self._rebase_flow(S)
            return o0 + r0, o1 + r1
        return self.call(f)

    def _move(self, move_to):
        """The vault's own spot move (repair C): an exact-output swap from idle toward `move_to` with price limit
        `move_to`; the input is paid from idle (SwapCap if short); slot0 must equal `move_to` afterwards."""
        pool = self.pool
        if pool.s != move_to:
            ain, aout, zfo = pool.push_to(move_to)
            if ain > (self.h.idle0 if zfo else self.h.idle1):
                raise Revert("SwapCap")
            if zfo:
                self.h.idle0 -= ain
                self.h.idle1 += aout
            else:
                self.h.idle1 -= ain
                self.h.idle0 += aout
        if pool.s != move_to:
            raise Revert("Move")

    def _remove_old(self):
        if self.h.L > 0:
            a0, a1, _, _ = self.pool.modify(self.KEY, self.h.lo, self.h.hi, -self.h.L)
            self.h.idle0 -= a0
            self.h.idle1 -= a1
            self.h.L = 0

    def _action_common(self, entry, now, ts, move_to=None, remove=False):
        """Prologue, mode gate, s0, (setPositions) removal of the old position and the optional move, s_spot, the
        live reference, fence and cooldown. Returns (obs, tick_spot, s_spot, ref, s0, before) with `before` the
        holdings before the removal and the move."""
        o, (lr, lt) = self.prologue(now, ts)
        self._gate(entry)
        s0 = self.pool.s
        before = self.h.copy()
        if remove:
            self._remove_old()
        if move_to:
            self._move(move_to)
        tick_spot, s_spot = self.pool.tick, self.pool.s
        ref = lt if lr else self.hook.live_ref(ts)
        if abs(tick_spot - ref) > self.t.REF_BAND:
            raise Revert("Fence")
        if self.lastAction and now - self.lastAction < self.t.COOLDOWN:
            raise Revert("Cooldown")
        if self.pending is not None:
            raise Revert("MarkoutPending")   # unreachable under the tier's COOLDOWN assertion (defensive)
        return o, tick_spot, s_spot, ref, s0, before

    def _swap_from_idle(self, zfo, amount_in, tick_spot, s_ref, nav_ref):
        """Shared by rebalanceSwap and the setPositions pre-swap: caps on the requested amount, the price
        limit at tick_spot ∓ (FEE_TICKS + BAND_SWAP), counters on the filled amount."""
        t = self.t
        if self.swapCount >= t.MAX_SWAPS:
            raise Revert("MaxSwaps")
        req_val = q(amount_in, s_ref) if zfo else amount_in
        frac = mul_div_up(req_val, WAD, nav_ref) if nav_ref else U64
        if frac > t.MAX_SWAP_ACTION or self.epochSwapFrac + frac > t.MAX_SWAP_EPOCH:
            raise Revert("SwapCap")
        if amount_in > (self.h.idle0 if zfo else self.h.idle1):
            raise Revert("SwapCap")
        lim_tick = tick_spot - (t.FEE_TICKS + t.BAND_SWAP) if zfo else tick_spot + (t.FEE_TICKS + t.BAND_SWAP)
        used, out = self.pool.swap(zfo, amount_in, get_sqrt_price_at_tick(lim_tick))
        if zfo:
            self.h.idle0 -= used
            self.h.idle1 += out
        else:
            self.h.idle1 -= used
            self.h.idle0 += out
        fill_val = q(used, s_ref) if zfo else used
        self.epochSwapFrac += mul_div_up(fill_val, WAD, nav_ref) if nav_ref else 0
        self.swapCount += 1
        self._collect_first()

    def _charge(self, before, s0, s_spot, s_ref, kind, max_charge, ts):
        """Immediate charge over {s_spot, s0, s_ref}, the fixed reserve, maxCharge, the gate, the burn, the reserve
        move and the pending record (repair C, §3.4 steps 7–9)."""
        reserve = mul_div(self.t.RESERVE_RATIO, value(before, s_spot)[0], WAD)
        charge, p_star, nav_after, burn, burn_r, ok = measure_c(before, self.h, s0, s_spot, s_ref, reserve, self.S,
                                                                self.bank.unvested(), self.bank.bondShares)
        if not ok:
            raise Revert("Insolvent")
        if charge + reserve > max_charge:
            raise Revert("MaxCharge")
        bb, _, bank_b, bond_b = fee_bank_apply(self.bank, "Charge", dict(burn=burn, burnR=burn_r))
        self.S -= bb
        r = self.rec(self.epoch)
        r["charge"] += charge
        r["bankBurned"] += bank_b
        r["bondBurned"] += bond_b
        self.events.append(("Action", kind, charge, p_star, bank_b, bond_b))
        self.pending = dict(tAction=ts, before=before.copy(), after=self.h.copy(), charge0=charge, reserve=reserve)
        return charge, p_star, burn

    NO_PRESWAP = {"active": False, "zeroForOne": False, "amountIn": 0}

    def set_positions(self, lo, hi, pre, now, ts, move_to=None, max_charge=U256):
        """setPositions(lo, hi, PreSwap, moveTo, maxCharge): prologue, s0, the vault's own move, guards, remove,
        optional pre-swap, add, charge and reserve, register. Returns (charge, pStar, burn, deltaAfter)."""
        def f():
            o, tick_spot, s_spot, ref, s0, before = self._action_common("SetPositions", now, ts, move_to, remove=True)
            t = self.t
            code = placement_check(lo, hi, tick_spot, ref, t.REF_BAND, t.W_TICKS, t.SPACING)
            if code:
                raise Revert(PLACEMENT_ERR[code])
            s_ref = get_sqrt_price_at_tick(ref)
            nav_ref = value(before, s_ref)[0]
            if pre["active"]:
                self._swap_from_idle(pre["zeroForOne"], pre["amountIn"], tick_spot, s_ref, nav_ref)
            L, after, d_after = placement_size(self.h, lo, hi, self.pool.s, s_ref)
            if L > 0:  # settle what the pool takes for L at its current price (equal to the sized amounts)
                a0, a1, _, _ = self.pool.modify(self.KEY, lo, hi, L)
                after = Holdings(self.h.idle0 - a0, self.h.idle1 - a1, L, lo, hi)
            self.h = after
            charge, p_star, burn = self._charge(before, s0, s_spot, s_ref, "setPositions", max_charge, ts)
            if d_after < t.DELTA_MIN:
                raise Revert("DeltaMin")
            self.hook.set_vault_range(lo, hi, ref, now, ts)
            fee_bank_apply(self.bank, "Place", {})
            self.lastAction = now
            self.NAV0, self.delta0 = self.nav(s_ref), delta_of(self.h, s_ref)
            return charge, p_star, burn, d_after
        return self.call(f)

    def rebalance_swap(self, zfo, amount_in, now, ts, max_charge=U256):
        def f():
            o, tick_spot, s_spot, ref, s0, before = self._action_common("RebalanceSwap", now, ts)
            s_ref = get_sqrt_price_at_tick(ref)
            self._swap_from_idle(zfo, amount_in, tick_spot, s_ref, self.nav(s_ref))
            charge, p_star, burn = self._charge(before, s0, s_spot, s_ref, "rebalanceSwap", max_charge, ts)
            self.lastAction = now
            self.NAV0, self.delta0 = self.nav(s_ref), delta_of(self.h, s_ref)
            return charge, p_star, burn
        return self.call(f)

    def poke(self, now, ts):
        return self.call(lambda: (self.prologue(now, ts), self._gate("Poke")))

    def settle(self, now, ts):
        """settle(): open; the prologue only (settlement step 0, then the checkpoint)."""
        return self.call(lambda: (self.prologue(now, ts), self._gate("Settle")))

    def process_breach(self, now, ts):
        return self.call(lambda: (self.prologue(now, ts), self._gate("ProcessBreach")))

    def claim(self, now, ts):
        def f():
            self.prologue(now, ts)
            self._gate("Claim")
            return fee_bank_apply(self.bank, "Claim", {})[1]
        return self.call(f)

    def retire(self, now, ts):
        def f():
            self.prologue(now, ts)
            self._gate("Retire")
            fv = self.baseOwed
            self.baseOwed = 0
            burned, _, forfeited, _ = fee_bank_apply(self.bank, "Retire", dict(now=now))
            self.S -= burned
            self.rec(now // self.t.EPOCH)["forfeitValue"] += fv
            return forfeited
        return self.call(f)

    def top_up_bond(self, shares, now, ts):
        def f():
            self.prologue(now, ts)
            self._gate("TopUpBond")
            fee_bank_apply(self.bank, "TopUp", dict(shares=shares))
        return self.call(f)

    def release_bond(self, now, ts):
        def f():
            self.prologue(now, ts)
            self._gate("ReleaseBond")
            return fee_bank_apply(self.bank, "Release", dict(shares=self.bank.bondShares, now=now))[1]
        return self.call(f)


if __name__ == "__main__":
    import selfcheck  # noqa: E402
    import vectors  # noqa: E402
    if "--vectors" in sys.argv:
        vectors.main()
    if "--selfcheck" in sys.argv or len(sys.argv) == 1:
        sys.exit(selfcheck.main())
