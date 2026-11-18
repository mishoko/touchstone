"""Self-check of the Touchstone reference model: property checks plus end-to-end scenarios at the TEST tier.

Each check asserts a rule of the specification; a failing check names it. Run:
    python3 model/reference.py --selfcheck
"""
from fractions import Fraction
import random

import reference as R
from v4math import get_sqrt_price_at_tick as sqrt_at, U256

T0 = 196250
USDC, ETH = 10 ** 6, 10 ** 18
FAILS = []
N_CHECKS = [0]


def check(cond, name, detail=""):
    N_CHECKS[0] += 1
    if not cond:
        FAILS.append(f"{name}: {detail}")


# ------------------------------------------------------------------ world driver
class World:
    """A TEST-tier world: a pool with third-party depth, a scripted reference, a vault and a clock."""

    def __init__(self, tier=R.TEST, ref_fn=None, third_L=10 ** 19, seed_amounts=(150_000 * USDC, 50 * ETH),
                 start_block=12_000, ref_fail=None, phase=0):
        self.t = tier
        self.phase = phase  # seconds added to every block timestamp (anchors need not align with blocks)
        self.ref_tick = ref_fn or (lambda ts: T0)
        self.fail = set() if ref_fail is None else ref_fail
        self.ref = R.RefSource(lambda ts: self.ref_tick(ts), self.fail)
        pool = R.Pool(sqrt_at(T0), tier.FEE)
        if third_L:
            pool.modify("lp", T0 - 6000, T0 + 6000, third_L)
        self.v = R.Vault(tier, self.ref, pool, sqrt_ref_price=sqrt_at(T0))
        self.block = start_block
        self.v.seed(*seed_amounts, now=self.block, ts=self.ts())
        self.depositor = 0

    def ts(self, b=None):
        return (self.block if b is None else b) * self.t.BLOCKTIME + getattr(self, "phase", 0)

    def step(self, n=1):
        self.block += n

    def touch(self):
        """A permissionless refreshRef() at the current block."""
        self.v.hook.refresh_ref(self.block, self.ts(), self.v.pool.s, self.v.pool.tick)

    def keepalive_run(self, blocks, every=None):
        every = every or self.t.GRID
        end = self.block + blocks
        while self.block < end:
            self.step(min(every, end - self.block))
            self.touch()

    def touch_same_block(self):
        """refreshRef() in the current block (no block advance)."""
        self.v.hook.refresh_ref(self.block, self.ts(), self.v.pool.s, self.v.pool.tick)

    def place(self, lo, hi, pre=None):
        return self.v.set_positions(lo, hi, pre if pre is not None else R.Vault.NO_PRESWAP, now=self.block, ts=self.ts())

    def poke(self):
        return self.v.poke(self.block, self.ts())

    def settle_due(self):
        """Repair C: deposits revert while an action's markout is pending; step to the settlement and settle."""
        pd = self.v.pending
        if pd is not None:
            due = pd["tAction"] + 2 * self.t.BLOCKTIME + self.t.H_MARKOUT
            if self.ts() < due:
                self.step(-(-(due - self.ts()) // self.t.BLOCKTIME))
            self.v.settle(self.block, self.ts())

    def deposit(self, shares):
        self.settle_due()
        r = self.v.deposit(shares, self.block, self.ts())
        self.depositor += shares
        return r

    def withdraw(self, shares):
        r = self.v.withdraw(shares, self.block, self.ts())
        self.depositor -= shares
        return r

    def claim(self):
        x = self.v.claim(self.block, self.ts())
        self.claimed = getattr(self, "claimed", 0) + x
        return x

    def release(self):
        x = self.v.release_bond(self.block, self.ts())
        self.claimed = getattr(self, "claimed", 0) + x
        return x


def conserved(w, name):
    """Share supply = DEAD + depositors + bank buckets + bond + forfeit pool + shares paid to the curator."""
    b = w.v.bank
    held = (R.DEAD + w.depositor + b.cohort_total() + b.maturedPending + b.vestedUnclaimed + b.bondShares +
            b.forfeitPool + b.reserveShares + getattr(w, "claimed", 0))
    check(w.v.S == held, f"share conservation ({name})", (w.v.S, held))


# ------------------------------------------------------------------ property checks
def props_ledger(rng):
    for i in range(400):
        lo = rng.randrange(T0 - 3000, T0 + 1000, 10)
        h = R.Holdings(rng.randint(0, 10 ** 13), rng.randint(0, 10 ** 22), rng.randint(0, 10 ** 22), lo, lo + 2000)
        sp, sr = sqrt_at(T0 + rng.randint(-60, 60)), sqrt_at(T0 + rng.randint(-60, 60))
        c, p, n, burn, ok = R.measure(h, h.copy(), sp, sr, 10 ** 24, 0, 0)
        check(c == 0 and burn == 0 and ok and p == sp, "zero charge for a no-op action", (c, burn))
        h2 = h.copy()
        h2.idle1 = max(0, h2.idle1 - rng.randint(1, 10 ** 18))
        S, unv, bond = rng.randint(10 ** 20, 10 ** 26), rng.randint(0, 10 ** 22), rng.randint(0, 10 ** 22)
        c, p, n, burn, ok = R.measure(h, h2, sp, sr, S, unv, bond)
        nb_s, na_s = R.value(h, sp)[0], R.value(h2, sp)[0]
        nb_r, na_r = R.value(h, sr)[0], R.value(h2, sr)[0]
        check(c == max(0, nb_s - na_s, nb_r - na_r), "charge = max(0, worse-of loss)")
        if c > 0 and n > 0:
            check(burn == -((-c * S) // n), "burn = ceilDiv(charge·S, NAV')")
            check(burn * n >= c * S and (burn - 1) * n < c * S, "burn is the least share count covering the charge")
            check(ok == (burn <= unv + bond), "gate in shares at the same price NAV'")
            check(n == min(na_s, na_r), "navBurn is the smallest NAV_after over the legs")
    # the 0 floor: a gain at both prices is never paid out (charge 0, burn 0), and no netting across actions
    for _ in range(50):
        h = R.Holdings(rng.randint(1, 10 ** 13), rng.randint(1, 10 ** 22), 0, 0, 0)
        g = h.copy()
        g.idle1 += rng.randint(1, 10 ** 18)
        c, p, n, burn, ok = R.measure(h, g, sqrt_at(T0), sqrt_at(T0 + 30), 10 ** 24, 0, 0)
        check(c == 0 and burn == 0 and ok, "0 floor: a gain is never credited", c)
    # monotone: lowering idle never lowers the charge
    h = R.Holdings(10 ** 12, 10 ** 21, 10 ** 20, T0 - 1000, T0 + 1000)
    prev = -1
    for d in range(0, 10 ** 18, 10 ** 17):
        a = h.copy()
        a.idle1 -= d
        c = R.measure(h, a, sqrt_at(T0), sqrt_at(T0 + 20), 10 ** 24, 0, 10 ** 24)[0]
        check(c >= prev, "charge monotone in the value removed")
        prev = c


def props_mint(rng):
    for _ in range(5000):
        nav = rng.randint(10 ** 18, 10 ** 30)
        S = rng.randint(nav, nav * 10 ** 6)
        A = rng.randint(1, nav // 3)
        sh = A * S // (nav - A)
        valued = sh * nav // (S + sh)
        check(A - 1 <= valued <= A, "minted value = base (±1 wei)", (nav, S, A, valued))


def props_bank(rng):
    for trial in range(300):
        b = R.Bank()
        b.dripBlocks, b.releaseDelay, b.bondShares = 120, 480, rng.randint(0, 10 ** 22)
        b.placedByCurator = True
        minted = burned = vested = forfeited_in = dripped = 0
        bond0 = b.bondShares
        now = 0
        for step in range(60):
            now += rng.randint(1, 80)
            e = now // 120
            op = rng.choice(["Accrue", "Accrue", "VestTime", "Charge", "Drip", "Breach", "Claim", "TopUp"])
            u0, pool0 = b.unvested(), b.forfeitPool
            try:
                if op == "Accrue":
                    x = rng.randint(0, 10 ** 21)
                    _, _, a, _ = R.fee_bank_apply(b, op, dict(epoch=e, shares=x, gateOpen=rng.random() < 0.5))
                    minted += x
                    vested += a
                elif op == "VestTime":
                    _, _, a, _ = R.fee_bank_apply(b, op, dict(nowEpoch=e, gateOpen=rng.random() < 0.5))
                    vested += a
                elif op == "Charge":
                    x = rng.randint(0, b.unvested() + b.bondShares + 10)
                    young = max((c[0] for c in b.cohorts if c[1] > 0), default=None)
                    young_sh = sum(c[1] for c in b.cohorts if c[0] == young and c[1] > 0) if young is not None else 0
                    bs, _, bank_b, bond_b = R.fee_bank_apply(b, op, dict(burn=x))
                    check(bank_b + bond_b == x, "charge burns exactly `burn`")
                    check(bond_b == 0 or b.unvested() == 0, "bond burned only after all unvested")
                    if young is not None and x > 0:
                        check(min(x, young_sh) == young_sh - sum(c[1] for c in b.cohorts if c[0] == young),
                              "youngest cohort burned first")
                    burned += bank_b
                    bond0 -= bond_b
                elif op == "Drip":
                    bs, _, _, _ = R.fee_bank_apply(b, op, dict(now=now))
                    dripped += bs
                elif op == "Breach":
                    bs, _, f, _ = R.fee_bank_apply(b, op, dict(count=1, now=now))
                    dripped += bs
                    forfeited_in += f
                    check(b.unvested() == 0, "breach forfeits every unvested cohort and maturedPending")
                elif op == "Claim":
                    R.fee_bank_apply(b, op, {})
                elif op == "TopUp":
                    x = rng.randint(0, 10 ** 20)
                    R.fee_bank_apply(b, op, dict(shares=x))
                    bond0 += x
            except R.Revert as ex:
                check(ex.err in ("Insolvent", "BadMode"), "only expected reverts", ex.err)
                if ex.err == "BadMode":
                    continue
            check(b.unvested() == minted - burned - vested - forfeited_in, "bank conservation (unvested)",
                  (trial, step, op))
            check(b.bondShares == bond0, "bond conservation")
            check(b.forfeitPool == forfeited_in - dripped, "forfeit pool conservation")
            check(all(c[1] >= 0 for c in b.cohorts) and b.maturedPending >= 0 and b.forfeitPool >= 0, "no negatives")
            check(b.forfeitPool <= b.forfeitTotal or b.forfeitTotal == 0, "pool <= drip target")


def props_drip(rng):
    for _ in range(500):
        b = R.Bank()
        b.dripBlocks = rng.randint(1, 50_000)
        T = rng.randint(1, 10 ** 24)
        b.forfeitPool = b.forfeitTotal = T
        b.forfeitStart = 1000
        total = 0
        now = 1000
        while now < 1000 + b.dripBlocks + 5:
            now += rng.randint(1, max(1, b.dripBlocks // 7))
            total += R.fee_bank_apply(b, "Drip", dict(now=now))[0]
            el = min(now - 1000, b.dripBlocks)
            check(total == min(T, T * el // b.dripBlocks), "drip follows the cumulative target", (T, now))
        check(b.forfeitPool == 0, "drip burns everything by the end of the epoch")


def props_saturation():
    b = R.Bank()
    b.vestedUnclaimed = R.U128 - 5
    b.cohorts[0] = [0, 100]
    R.fee_bank_apply(b, "VestTime", dict(nowEpoch=10, gateOpen=True))
    check(b.vestedUnclaimed == R.U128, "uint128 bank arithmetic saturates (never reverts)")
    b2 = R.Bank()
    b2.bondShares = R.U128 - 1
    R.fee_bank_apply(b2, "TopUp", dict(shares=10))
    check(b2.bondShares == R.U128, "topUp saturates")
    b3 = R.Bank()
    b3.forfeitPool = b3.forfeitTotal = 10
    b3.dripBlocks = 0
    try:
        burned0 = R.fee_bank_apply(b3, "Drip", dict(now=5))[0]
    except Exception as ex:  # a targeted unit check: any exception here is the failure being tested
        burned0 = repr(ex)
    check(burned0 == 10, "dripBlocks 0 burns all without dividing by zero", burned0)
    m, br, ex = R.ladder_next(R.EXITED, R.U32, R.EV_BREACH, 5)
    check(br == R.U32 and m == R.EXITED and not ex, "breach counter saturates")
    h = R.Hook(R.TEST, R.RefSource(lambda ts: T0))
    h.ack_breaches(5)
    check(h.pendingBreaches == 0, "ackBreaches saturates at 0")
    check(R.measure(R.Holdings(10, 10), R.Holdings(), sqrt_at(T0), sqrt_at(T0), 10, 10 ** 30, 10 ** 30)[3] == U256,
          "burn saturates when NAV' = 0")


def props_ladder():
    for mode in (R.NORMAL, R.BREACHED, R.EXITED):
        for b in range(0, 5):
            for c in range(0, 4):
                m, br, ex = R.ladder_next(mode, b, R.EV_BREACH, c)
                if c == 0:
                    check((m, br, ex) == (mode, b, False), "Breach(0) is the identity")
                elif mode == R.EXITED:
                    check((m, br, ex) == (R.EXITED, b + c, False), "EXITED stays EXITED, exited = false")
                else:
                    check((m, ex) == ((R.EXITED, True) if b + c >= 3 else (R.BREACHED, False)), " breach rows")
            for ev in (R.EV_PLACE, R.EV_RETIRE):
                try:
                    m, br, ex = R.ladder_next(mode, b, ev, 0)
                    check(mode != R.EXITED, "Place/Retire from EXITED revert")
                    check((m, ex) == ((R.NORMAL, False) if ev == R.EV_PLACE else (R.EXITED, True)), " rows")
                except R.Revert as ex_:
                    check(mode == R.EXITED and ex_.err == "BadMode", "BadMode only from EXITED")
    table = {"Deposit": "110", "Withdraw": "111", "SetPositions": "110", "RebalanceSwap": "110", "Claim": "111",
             "TopUpBond": "110", "Retire": "110", "ReleaseBond": "001", "Poke": "111", "ProcessBreach": "111"}
    for entry, row in table.items():
        for mode in range(3):
            check(R.ladder_allowed(mode, R.E[entry]) == (row[mode] == "1"), " entry table", (entry, mode))


def props_units():
    # vest boundary: a cohort of epoch e vests at nowEpoch = e + 1 + V, not before
    for e in (0, 3, 17):
        b = R.Bank()
        b.cohorts[e % R.RING] = [e, 100]
        R.fee_bank_apply(b, "VestTime", dict(nowEpoch=e + R.V, gateOpen=True))
        check(b.vestedUnclaimed == 0, "no vesting at nowEpoch = e + V")
        R.fee_bank_apply(b, "VestTime", dict(nowEpoch=e + 1 + R.V, gateOpen=True))
        check(b.vestedUnclaimed == 100, "vesting at nowEpoch = e + 1 + V")
    # ring overwrite: the occupant leaves the slot (vested or pending), the slot holds only the new epoch
    for gate in (True, False):
        b = R.Bank()
        b.cohorts[2] = [2, 40]
        _, _, a, b2 = R.fee_bank_apply(b, "Accrue", dict(epoch=7, shares=5, gateOpen=gate))
        check(b.cohorts[2] == [7, 5], "ring slot rewritten for the new epoch", b.cohorts[2])
        check((a, b2) == ((40, 0) if gate else (0, 40)), "occupant vests (gate open) or moves to maturedPending")
        check(b.vestedUnclaimed + b.maturedPending == 40, "occupant conserved")
    # latch block: lastPendingBlock = lastSnap + (CURE − old), τ_v at it
    t = R.TEST
    for old in (0, 150, 199, 185):
        h = R.Hook(t, R.RefSource(lambda ts: T0 + 3000))
        h.armed, h.vaultLo, h.vaultHi, h.emptyRange = True, T0 - 1000, T0 + 1000, False
        h.refTick, h.refGrid, h.refCachedAt, h.lastSnap, h.outBlocks, h.tauV = T0 + 3000, 10 ** 9, 1000, 1000, old, 77
        h.ring = [(1000, T0)]
        h.snapshot(1000 + 2 * t.GRID, 10 ** 12, sqrt_at(T0), T0)
        crossed = old + 2 * t.GRID >= t.CURE_BLOCKS
        check(h.pendingBreaches == (1 if crossed else 0), "latch iff the span crosses CURE", (old, h.pendingBreaches))
        if crossed:
            check(h.lastPendingBlock == 1000 + (t.CURE_BLOCKS - old), "lastPendingBlock is the crossing block",
                  (old, h.lastPendingBlock))
            check(h.tauVAtLastPending == 77, "τ_v at the latch (τ_v and outBlocks never both advance)")
    # stale span: credit truncated at refCachedAt + 2·GRID; the gap flag set iff now > refCachedAt + 2·GRID
    for gap in (2 * t.GRID - 1, 2 * t.GRID, 2 * t.GRID + 1, 10 * t.GRID):
        h = R.Hook(t, R.RefSource(lambda ts: T0))
        h.armed, h.vaultLo, h.vaultHi, h.emptyRange = True, T0 - 1000, T0 + 1000, False
        h.refTick, h.refGrid, h.refCachedAt, h.lastSnap = T0, 10 ** 9, 1000, 1000
        h.ring = [(1000, T0)]
        h.snapshot(1000 + gap, 10 ** 12, sqrt_at(T0), T0)
        check(h.tauV == min(gap, 2 * t.GRID), "τ_v credit truncated at the cache freshness", (gap, h.tauV))
        check(h.gapSinceVerify == (gap > 2 * t.GRID), "gap flag iff now > refCachedAt + 2·GRID", gap)
    # τ_v needs the pool price strictly inside: a price exactly at sqrtAt(lo) earns nothing
    for sp, want in ((sqrt_at(T0 - 1000), 0), (sqrt_at(T0 - 1000) + 1, 20), (sqrt_at(T0 + 1000) - 1, 20),
                     (sqrt_at(T0 + 1000), 0)):
        h = R.Hook(t, R.RefSource(lambda ts: T0))
        h.armed, h.vaultLo, h.vaultHi, h.emptyRange = True, T0 - 1000, T0 + 1000, False
        h.refTick, h.refGrid, h.refCachedAt, h.lastSnap = T0, 10 ** 9, 1000, 1000
        h.ring = [(1000, T0)]
        h.snapshot(1020, 10 ** 12, sp, R.get_tick_at_sqrt_price(sp))
        check(h.tauV == want, "τ_v strictly inside the range", (sp, h.tauV))
    # EMPTY counts as out of range
    h = R.Hook(t, R.RefSource(lambda ts: T0))
    h.armed, h.refTick, h.refGrid, h.refCachedAt, h.lastSnap, h.ring = True, T0, 10 ** 9, 1000, 1000, [(1000, T0)]
    h.snapshot(1020, 10 ** 12, sqrt_at(T0), T0)
    check(h.outBlocks == 20 and h.tauV == 0, "EMPTY range: the clock counts, no τ_v")


def props_placement(rng):
    for _ in range(300):
        lo = rng.randrange(T0 - 2500, T0 + 500, 10)
        idle = R.Holdings(rng.randint(0, 10 ** 13), rng.randint(0, 10 ** 22))
        sp = sqrt_at(T0 + rng.randint(-60, 60)) + rng.randint(0, 10 ** 6)
        sr = sqrt_at(T0 + rng.randint(-60, 60))
        L, after, d = R.placement_size(idle, lo, lo + 2000, sp, sr)
        check(after.idle0 <= idle.idle0 and after.idle1 <= idle.idle1, "placement uses only idle")
        check(after.L == L and d <= R.WAD, "placement output consistent")
        # spot-neutrality: a no-swap placement at spot changes NAV(spot) by rounding only (≤ 2 wei + headroom)
        nb = R.value(idle, sp)[0]
        na = R.value(after, sp)[0]
        tol = R.q(2, sp) + 3  # one raw unit of each token per add (round up) and per valuation (floor)
        check(na <= nb and nb - na <= tol, "a no-swap placement is value-neutral at spot up to rounding", (nb - na, tol))
    check(R.placement_check(T0 - 1000, T0 + 1000, T0 + 61, T0, 60, 2000, 10) == 1, "fence first")
    check(R.placement_check(T0 - 995, T0 + 1000, T0, T0, 60, 2000, 10) == 2, "width before alignment")


# ------------------------------------------------------------------ scenarios
def scenario_base_and_vesting():
    """A placed vault kept alive in range: base accrues with δ·Δτ_v; cohorts vest after V epochs only while
    the gate is open; minted value = base."""
    w = World()
    w.place(T0 - 1000, T0 + 1000)
    check(w.v.bank.mode == R.NORMAL and w.v.bank.placedByCurator, "placement sets NORMAL and placedByCurator")
    check(not w.v.hook.emptyRange and not w.v.hook.gapSinceVerify, "registration clears EMPTY and the gap flag")
    w.deposit(10 ** 26)
    S0 = w.v.S
    tau0 = w.v.tauV0
    navref = w.v.nav(sqrt_at(T0))
    w.keepalive_run(100)
    o = w.v.hook.obs(w.block, 0)
    expected = R.mul_div(R.mul_div(w.t.m_perBlock, w.v.NAV0, R.WAD), w.v.delta0 * (o["tauV"] - tau0), R.WAD)
    check(o["tauV"] - tau0 == 100, "τ_v counts every observed in-range block", o["tauV"] - tau0)
    w.poke()
    minted = w.v.S - S0
    value_minted = minted * navref // w.v.S
    check(abs(value_minted - expected) <= 1 + navref // w.v.S, "minted value = base (±1 wei)", (value_minted, expected))
    # vest after V+1 epochs with keep-alive; nothing vests earlier
    e_mint = w.block // w.t.EPOCH
    w.keepalive_run(w.t.EPOCH * (1 + R.V) - (w.block % w.t.EPOCH) - 1)
    w.poke()
    check(w.v.bank.vestedUnclaimed == 0 or (w.block // w.t.EPOCH) >= e_mint + 1 + R.V, "no early vesting")
    w.keepalive_run(2)
    w.poke()
    check(w.v.bank.vestedUnclaimed > 0, "a matured cohort vests at a checkpoint with the gate open")
    check(w.v.live_reads >= 1, "the live leg is read when something is vestable")
    paid = w.claim()
    check(paid > 0 and w.v.bank.vestedUnclaimed == 0, "claim pays vested shares")
    conserved(w, "base and vesting")
    return w


def scenario_live_leg():
    """Cache in range but the live reference outside: nothing vests (the live leg closes the gate)."""
    st = {"live_out": False}
    w = World(ref_fn=lambda ts: T0)
    w.place(T0 - 1000, T0 + 1000)
    w.keepalive_run(20)
    w.poke()
    w.keepalive_run(w.t.EPOCH * (2 + R.V))
    # make the live read return out of range only at the poke block (cache still the in-range anchor)
    poke_ts = w.ts(w.block + 1)
    w.ref_tick = lambda ts: T0 + 5000 if ts == poke_ts else T0
    w.step(1)
    reads0 = w.v.live_reads
    w.poke()
    check(w.v.live_reads == reads0 + 1, "exactly one live read when vestable")
    check(w.v.bank.vestedUnclaimed == 0 and w.v.bank.maturedPending > 0, "live leg out: matured pay -> maturedPending")
    w.ref_tick = lambda ts: T0
    w.step(1)
    w.poke()
    check(w.v.bank.vestedUnclaimed > 0 and w.v.bank.maturedPending == 0, "maturedPending vests once the gate opens")
    # unreadable live reference: the gate closes, nothing reverts
    w.keepalive_run(w.t.EPOCH)
    w.poke()
    vu = w.v.bank.vestedUnclaimed
    w.keepalive_run(w.t.EPOCH * (1 + R.V))
    fail_ts = w.ts(w.block + 1)
    w.fail.add(fail_ts)
    w.step(1)
    w.poke()
    check(w.v.bank.vestedUnclaimed == vu, "a failed live read closes the gate and does not revert")
    del st
    return w


def scenario_no_read_when_not_vestable():
    w = World()
    w.place(T0 - 1000, T0 + 1000)
    r0 = w.v.live_reads
    for _ in range(3):
        w.keepalive_run(10)
        w.poke()
    check(w.v.live_reads == r0, "no live read when nothing is vestable")


def scenario_breach_and_removal():
    """The reference leaves the range: the clock counts observed out-of-range time, latches one breach per
    CURE_BLOCKS, the checkpoint forfeits unvested pay, and three breaches remove the curator."""
    st = {"tick": T0}
    w = World(ref_fn=lambda ts: st["tick"])
    w.place(T0 - 1000, T0 + 1000)
    w.deposit(10 ** 26)
    w.keepalive_run(w.t.EPOCH)
    w.poke()
    check(w.v.bank.cohort_total() > 0, "base minted into a cohort")
    st["tick"] = T0 + 1500  # reference out of the range
    w.keepalive_run(w.t.CURE_BLOCKS - w.t.GRID)
    check(w.v.hook.pendingBreaches == 0, "no breach before CURE_BLOCKS of observed out-of-range time",
          w.v.hook.outBlocks)
    w.keepalive_run(3 * w.t.GRID)
    check(w.v.hook.pendingBreaches == 1 and w.v.hook.breachSeq == 1, "one breach latched at CURE",
          (w.v.hook.pendingBreaches, w.v.hook.outBlocks))
    unv = w.v.bank.unvested()
    w.poke()
    check(w.v.bank.mode == R.BREACHED and w.v.bank.breaches == 1, "processed: BREACHED")
    check(w.v.bank.unvested() == 0 and w.v.bank.forfeitPool == unv, "all unvested forfeited to the drip pool")
    check(w.v.hook.pendingBreaches == 0, "ackBreaches subtracts the processed count")
    # the run continues out of range: a further breach every CURE_BLOCKS
    w.keepalive_run(w.t.CURE_BLOCKS + w.t.GRID)
    check(w.v.hook.pendingBreaches == 1 and w.v.hook.breachSeq == 2, "second breach after a further CURE_BLOCKS")
    w.poke()
    check(w.v.bank.breaches == 2 and w.v.bank.mode == R.BREACHED, "two breaches")
    # pending removal: the third breach latches; a curator action in the same block must revert BadMode
    w.keepalive_run(w.t.CURE_BLOCKS + w.t.GRID)
    check(w.v.hook.pendingBreaches == 1, "third breach pending")
    snapshot_bank = w.v.bank.d()
    for name, call in (("setPositions", lambda: w.place(T0 + 600, T0 + 2600)),
                       ("rebalanceSwap", lambda: w.v.rebalance_swap(True, 10, w.block, w.ts())),
                       ("topUpBond", lambda: w.v.top_up_bond(1, w.block, w.ts())),
                       ("retire", lambda: w.v.retire(w.block, w.ts())),
                       ("deposit", lambda: w.deposit(10 ** 18))):
        try:
            call()
            check(False, f"pending removal: {name} must revert")
        except R.Revert as ex:
            check(ex.err == "BadMode", f"pending removal: {name} reverts BadMode", ex.err)
    check(w.v.bank.d() == snapshot_bank and w.v.hook.pendingBreaches == 1, "the reverted calls leave the breach pending")
    w.poke()
    check(w.v.bank.mode == R.EXITED and w.v.bank.exitBlock == w.block and not w.v.bank.placedByCurator,
          "third breach: EXITED, exitBlock = now")
    exit_block = w.block
    # post-exit breach: exitBlock unchanged; release after V·EPOCH
    w.keepalive_run(w.t.CURE_BLOCKS + w.t.GRID)
    w.poke()
    check(w.v.bank.breaches == 4 and w.v.bank.exitBlock == exit_block, "post-exit breach keeps exitBlock")
    try:
        w.v.release_bond(w.block, w.ts())
        check(w.block >= exit_block + R.V * w.t.EPOCH, "release only after the delay")
    except R.Revert as ex:
        check(ex.err == "BondLocked", "release before the delay reverts BondLocked")
    w.keepalive_run(exit_block + R.V * w.t.EPOCH - w.block)
    bond = w.v.bank.bondShares
    got = w.release()
    check(got == bond and w.v.bank.bondShares == 0, "bond released at exitBlock + V·EPOCH")
    conserved(w, "breach and removal")
    return w


def scenario_reset_and_registration():
    st = {"tick": T0}
    w = World(ref_fn=lambda ts: st["tick"])
    w.place(T0 - 1000, T0 + 1000)
    st["tick"] = T0 + 1500
    w.keepalive_run(w.t.CURE_BLOCKS // 2)
    check(w.v.hook.outBlocks > 0, "clock counts while the cached reference is out of range")
    st["tick"] = T0
    w.keepalive_run(w.t.GRID * w.t.RESET_RUN + w.t.GRID)
    check(w.v.hook.outBlocks == 0, "RESET_RUN consecutive in-range anchors end the run")
    # a pool push alone never moves the clock
    st["tick"] = T0
    w.v.pool.push_to(sqrt_at(T0 + 1500))
    w.keepalive_run(w.t.CURE_BLOCKS + 3 * w.t.GRID)
    check(w.v.hook.pendingBreaches == 0 and w.v.hook.outBlocks == 0, "a pool push neither starts nor stops the clock")
    tv = w.v.hook.tauV
    w.keepalive_run(3 * w.t.GRID)
    check(w.v.hook.tauV == tv, "no τ_v while the pool price is outside the range")
    w.v.pool.push_to(sqrt_at(T0))
    # registration starts a run and clears outBlocks
    st["tick"] = T0 + 1500
    w.keepalive_run(w.t.CURE_BLOCKS // 2)
    w.v.pool.push_to(sqrt_at(T0 + 1500))
    w.keepalive_run(w.t.COOLDOWN)
    c1 = (T0 + 1500) // 10 * 10  # edge60, token1 side: [c'+spacing-W, c'+spacing)
    w.v.pool.push_to(sqrt_at(c1 + 10) - 1)  # spot one unit below sqrtAt(hi) (the empty-region move)
    w.place(c1 + 10 - 2000, c1 + 10)
    check(w.v.hook.outBlocks == 0 and w.v.hook.vaultLo == c1 + 10 - 2000, "registration starts a new run")


def scenario_gap():
    """No observation for longer than the cache freshness: the gap flag is set, the span is credited only up to
    refCachedAt + 2·GRID, nothing vests until a verified return (RESET_RUN anchors or a registration)."""
    w = World()
    w.place(T0 - 1000, T0 + 1000)
    w.keepalive_run(w.t.EPOCH)
    w.poke()
    tv = w.v.hook.tauV
    cached = w.v.hook.refCachedAt
    prev = w.v.hook.lastSnap
    w.step(w.t.EPOCH * (2 + R.V))  # silence
    w.poke()
    check(w.v.hook.gapSinceVerify, "a span past the cache freshness sets gapSinceVerify")
    check(w.v.hook.tauV - tv == cached + 2 * w.t.GRID - prev, "span credit truncated at freshness",
          (w.v.hook.tauV - tv))
    check(w.v.bank.vestedUnclaimed == 0 and w.v.bank.maturedPending > 0, "gap: matured pay waits in maturedPending")
    check(w.v.hook.resetStreak == 1, "the gap-closing anchor is the first of RESET_RUN", w.v.hook.resetStreak)
    w.keepalive_run(w.t.GRID // 2)
    w.poke()
    check(w.v.bank.vestedUnclaimed == 0 and w.v.hook.gapSinceVerify, "within the first anchor: still unverified")
    w.keepalive_run(w.t.GRID * (w.t.RESET_RUN - 1))
    w.poke()
    check(not w.v.hook.gapSinceVerify and w.v.bank.vestedUnclaimed > 0, "RESET_RUN in-range anchors verify the return")


def scenario_actions():
    w = World()
    w.place(T0 - 1000, T0 + 1000)
    w.deposit(10 ** 26)
    w.keepalive_run(w.t.COOLDOWN)
    # cooldown
    w.keepalive_run(1)
    w.place(T0 - 1010, T0 + 990)
    try:
        w.step(1)
        w.place(T0 - 1000, T0 + 1000)
        check(False, "cooldown must revert")
    except R.Revert as ex:
        check(ex.err == "Cooldown", "COOLDOWN between curator actions", ex.err)
    w.keepalive_run(w.t.COOLDOWN)
    # fence revert, width, containment, DeltaMin
    for args, err in (((T0 - 1000, T0 + 1010), "Width"), ((T0 - 995, T0 + 1005), "Alignment"),
                      ((T0 + 10, T0 + 2010), "NotContained")):
        try:
            w.place(*args)
            check(False, f"{err} must revert")
        except R.Revert as ex:
            check(ex.err == err, f"placement guard {err}", ex.err)
    w.v.pool.push_to(sqrt_at(T0 + 61))
    try:
        w.place(T0 - 900, T0 + 1100)
        check(False, "fence must revert")
    except R.Revert as ex:
        check(ex.err == "Fence", "fence |spot - ref| <= REF_BAND", ex.err)
    w.v.pool.push_to(sqrt_at(T0))
    # rebalanceSwap: caps, charge, mode unchanged
    nav = w.v.nav(sqrt_at(T0))
    big = R.mul_div(nav * 3 // 100, 1 << 192, sqrt_at(T0) ** 2)
    try:
        w.v.rebalance_swap(True, big, w.block, w.ts())
        check(False, "swap cap must revert")
    except R.Revert as ex:
        check(ex.err == "SwapCap", "MAX_SWAP_ACTION", ex.err)
    c, p, burn = w.v.rebalance_swap(True, min(big // 2, w.v.h.idle0), w.block, w.ts())
    act = [e for e in w.v.events if e[0] == "Action"][-1]
    check(c > 0 and burn > 0 and act[2] == c and w.v.bank.mode == R.NORMAL, "swap charged and burned; mode unchanged")
    # retire
    w.keepalive_run(w.t.COOLDOWN)
    f = w.v.retire(w.block, w.ts())
    check(w.v.bank.mode == R.EXITED and w.v.bank.unvested() == 0, "retire forfeits unvested and exits")
    try:
        w.deposit(10 ** 18)
        check(False, "deposit in EXITED reverts")
    except R.Revert as ex:
        check(ex.err == "BadMode", "deposit reverts in EXITED")
    out = w.withdraw(w.depositor // 2)
    check(out[0] > 0 and out[1] > 0, "withdraw in kind open in EXITED")
    conserved(w, "actions")
    del f


def scenario_deltamin():
    """After an exit above the range the vault holds mostly token1; a token0-side placement leaves most of
    the inventory idle and reverts DeltaMin; the token1-side edge placement succeeds."""
    st = {"tick": T0}
    w = World(ref_fn=lambda ts: st["tick"])
    w.place(T0 - 1000, T0 + 1000)
    w.deposit(10 ** 26)
    st["tick"] = T0 + 1500
    w.v.pool.push_to(sqrt_at(T0 + 1500))
    w.keepalive_run(3 * w.t.GRID)
    before = w.v.bank.d()
    c = (T0 + 1500) // 10 * 10
    try:
        w.place(c, c + 2000)
        check(False, "token0-side placement with token1 inventory must revert DeltaMin")
    except R.Revert as ex:
        check(ex.err == "DeltaMin", "dust placement reverts DeltaMin", ex.err)
        check(w.v.bank.d() == before, "revert leaves the bank unchanged")
    w.v.pool.push_to(sqrt_at(c + 10) - 1)
    ch, p, burn, d = w.place(c + 10 - 2000, c + 10)
    check(d >= w.t.DELTA_MIN, "edge placement on the inventory side meets DELTA_MIN", d)


def scenario_insolvent():
    """A charge above unvested + bond reverts Insolvent and leaves the vault unchanged. A tier with a far smaller
    BOND_RATIO lets NAV outgrow the bond; a swap buying token0 at a spot 60 ticks above the reference (the fence)
    is then uncoverable."""
    tier = R.Tier(**{**R.TEST.__dict__, "BOND_RATIO": 10 ** 9})
    w = World(tier=tier, third_L=10 ** 15, seed_amounts=(300_000 * USDC, 20 * ETH))
    w.place(T0, T0 + 2000)  # token0-side edge: token1 stays idle
    w.deposit(10 ** 30)
    w.keepalive_run(w.t.COOLDOWN)
    w.v.pool.push_to(sqrt_at(T0 + 60))
    nav = w.v.nav(sqrt_at(T0))
    amt = min(nav // 100, w.v.h.idle1)
    before, S, h = w.v.bank.d(), w.v.S, w.v.h.d()
    try:
        w.v.rebalance_swap(False, amt, w.block, w.ts())
        check(False, "an uncoverable charge must revert Insolvent")
    except R.Revert as ex:
        check(ex.err == "Insolvent", "solvency gate reverts Insolvent", ex.err)
        check(w.v.bank.d() == before and w.v.S == S and w.v.h.d() == h, "a reverted action leaves the vault unchanged")
    out = w.v.withdraw(10 ** 29, w.block, w.ts())
    check(out[0] > 0 or out[1] > 0, "the vault stays withdrawable after an insolvent attempt")
    # the same swap with an ample bond succeeds and burns exactly the gate's shares
    w2 = World(seed_amounts=(300_000 * USDC, 20 * ETH))
    w2.place(T0, T0 + 2000)
    w2.keepalive_run(w2.t.COOLDOWN)
    w2.v.pool.push_to(sqrt_at(T0 + 60))
    w2.settle_due()
    amt2 = min(w2.v.nav(sqrt_at(T0)) // 100, w2.v.h.idle1)
    bond0 = w2.v.bank.bondShares + w2.v.bank.unvested()
    c, p, burn = w2.v.rebalance_swap(False, amt2, w2.block, w2.ts())
    conserved(w2, "charge burn")
    check(c > 0 and p == sqrt_at(T0) and w2.v.bank.bondShares + w2.v.bank.unvested() == bond0 - burn - w2.v.bank.reserveShares,
          "fence swap charged at the reference leg; burn from bank then bond", (c, p))


def scenario_epilogue():
    w = World()
    w.place(T0 - 1000, T0 + 1000)
    w.settle_due()
    n0, s0 = w.v.NAV0, w.v.S
    w.deposit(10 ** 26)
    check(w.v.NAV0 == R.mul_div(n0, w.v.S, s0), "deposit epilogue re-bases NAV0·S'/S without a price")
    d0 = w.v.delta0
    w.v.withdraw(10 ** 25, w.block, w.ts())
    check(w.v.delta0 == d0, "in-kind flows leave δ0 unchanged")


def anchor_split(anchor_tick, live_tick, tier=R.TEST):
    """A reference whose anchored samples read `anchor_tick` and whose live reads read `live_tick`."""
    return lambda ts: anchor_tick(ts) if ts % tier.GRID_SEC == 0 else live_tick(ts)


def rules_pay():
    t = R.TEST
    # deferred base: no fresh cache and an unreadable live reference -> baseOwed; minted at the next priced checkpoint
    w = World()
    w.place(T0 - 1000, T0 + 1000)
    w.deposit(10 ** 26)
    w.keepalive_run(40)
    w.poke()
    w.keepalive_run(30)
    start = w.ts()
    w.ref.fail = lambda end: end > start
    w.step(3 * t.GRID)
    m0 = w.v.mint_reads
    S0 = w.v.S
    w.poke()
    check(w.v.mint_reads == m0 + 1, "stale cache: the mint price comes from tryRef")
    check(w.v.baseOwed > 0 and w.v.S == S0 and any("deferred" in r["flags"] for r in w.v.records.values()),
          "no price: base amount closed into baseOwed, record flagged deferred", w.v.baseOwed)
    owed = w.v.baseOwed
    w.ref.fail = set()
    w.keepalive_run(t.GRID * 3)
    S1, nav = w.v.S, None
    w.poke()
    check(w.v.baseOwed == 0 and w.v.S > S1, "baseOwed minted at the next priced checkpoint")
    # baseOwed is forfeited by a breach and by retire (checked with no price, so nothing can be minted)
    start = w.ts()
    w.ref.fail = lambda end: end > start
    w.step(3 * t.GRID)
    w.poke()
    owed = w.v.baseOwed
    check(owed > 0, "deferred base available for the forfeiture checks")
    h = w.v.hook
    h.pendingBreaches, h.lastPendingBlock, h.tauVAtLastPending = 1, w.block, h.tauV
    fv0 = sum(r["forfeitValue"] for r in w.v.records.values())
    w.step(1)
    w.poke()
    check(w.v.baseOwed < owed // 2 and sum(r["forfeitValue"] for r in w.v.records.values()) >= fv0 + owed,
          "a breach forfeits baseOwed", (w.v.baseOwed, owed))
    w.v.baseOwed = owed
    w.step(t.COOLDOWN)
    w.v.retire(w.block, w.ts())
    check(w.v.baseOwed == 0, "retire forfeits baseOwed")
    w.ref.fail = set()
    # no base in EXITED even while the range stays in range
    S2 = w.v.S
    w.keepalive_run(t.EPOCH)
    w.poke()
    check(w.v.bank.cohort_total() == 0 and w.v.S <= S2, "no base accrues in EXITED")
    # fresh cache: base minted at the cached reference, no live read
    st = {"live": T0}
    w = World(ref_fn=lambda ts: T0)
    w.place(T0 - 1000, T0 + 1000)
    w.deposit(10 ** 26)
    w.keepalive_run(60)
    w.poke()
    w.ref_tick = anchor_split(lambda ts: T0, lambda ts: T0 + 50)
    w.keepalive_run(40)
    v = w.v
    S0, tau0, m0 = v.S, v.tauV0, v.mint_reads
    w.step(1)
    o = v.hook.snapshot(w.block, w.ts(), v.pool.s, v.pool.tick)
    base = R.mul_div(R.mul_div(t.m_perBlock, v.NAV0, R.WAD), v.delta0 * (o["tauV"] - tau0), R.WAD)
    v._collect_first()
    navp = v.nav(sqrt_at(T0))
    v._checkpoint(o, w.block, w.ts())
    check(v.mint_reads == m0 and v.S - S0 == base * S0 // (navp - base), "fresh cache: mint at the cached reference")
    del st


def rules_gate_leg():
    """Cache out of range, live reference in range: the cached leg keeps the gate closed."""
    w = World(ref_fn=lambda ts: T0)
    w.place(T0 - 1000, T0 + 1000)
    w.keepalive_run(R.TEST.EPOCH * (2 + R.V))
    w.poke()
    w.keepalive_run(R.TEST.EPOCH * (1 + R.V))
    w.ref_tick = anchor_split(lambda ts: T0 + 1500, lambda ts: T0)
    w.step(7)  # touches 7 blocks after the anchor: an anchored read and a live read differ
    w.keepalive_run(R.TEST.GRID)
    vu, r0 = w.v.bank.vestedUnclaimed, w.v.live_reads
    w.step(1)
    w.poke()
    check(w.v.bank.vestedUnclaimed == vu and w.v.bank.maturedPending > 0 and w.v.live_reads == r0,
          "cached reference out of range: no vesting and no live read")


def rules_clock():
    t = R.TEST

    def hook(anchor_ticks, streak, out=50):
        h = R.Hook(t, R.RefSource(lambda ts: anchor_ticks.get(ts // t.GRID_SEC, T0)))
        h.armed, h.vaultLo, h.vaultHi, h.emptyRange = True, T0 - 1000, T0 + 1000, False
        h.refTick, h.refGrid, h.refCachedAt, h.lastSnap, h.ring = T0, 100, 2000, 2000, [(2000, T0)]
        h.resetStreak, h.outBlocks = streak, out
        return h
    # streak counts consecutive anchor indices only: skip anchor 101, sample 102 -> streak 1, no reset
    h = hook({}, 1)
    h.snapshot(2040, 2040 * t.BLOCKTIME, sqrt_at(T0), T0)
    check(h.resetStreak == 1 and h.outBlocks == 50, "streak restarts after a skipped anchor", (h.resetStreak, h.outBlocks))
    h.snapshot(2060, 2060 * t.BLOCKTIME, sqrt_at(T0), T0)
    check(h.outBlocks == 0 and not h.gapSinceVerify, "RESET_RUN consecutive in-range anchors reset the run")
    check(h.resetStreak == 0, "a reset ends the run and zeroes the streak")
    # an out-of-range sample zeroes the streak
    h = hook({101: T0 + 1500}, 1)
    h.snapshot(2020, 2020 * t.BLOCKTIME, sqrt_at(T0), T0)
    check(h.resetStreak == 0, "an out-of-range sample zeroes the streak")
    h.snapshot(2040, 2040 * t.BLOCKTIME, sqrt_at(T0), T0)
    check(h.resetStreak == 1 and h.outBlocks > 0, "no reset after out, in", (h.resetStreak, h.outBlocks))
    # at most one refresh per anchor
    h = hook({}, 0)
    h.snapshot(2020, 2020 * t.BLOCKTIME, sqrt_at(T0), T0)
    n = sum(1 for e in h.events if e[0] == "RefRefreshed")
    h.refresh_ref(2021, 2021 * t.BLOCKTIME, sqrt_at(T0), T0)
    h.refresh(2022, 2022 * t.BLOCKTIME)
    check(sum(1 for e in h.events if e[0] == "RefRefreshed") == n and h.refCachedAt == 2020,
          "one cache write per anchor")
    # outBlocks credit truncated at the cache freshness (EMPTY range, long gap)
    h = R.Hook(t, R.RefSource(lambda ts: T0))
    h.armed, h.refTick, h.refGrid, h.refCachedAt, h.lastSnap, h.ring = True, T0, 10 ** 9, 1000, 1000, [(1000, T0)]
    h.snapshot(1000 + 10 * t.GRID, 10 ** 12, sqrt_at(T0), T0)
    check(h.outBlocks == 2 * t.GRID, "outBlocks credit truncated at the cache freshness", h.outBlocks)
    # registration after a gap clears it and writes the cache
    w = World()
    w.place(T0 - 1000, T0 + 1000)
    w.step(10 * t.GRID)
    w.poke()
    check(w.v.hook.gapSinceVerify, "gap set")
    w.keepalive_run(t.COOLDOWN)
    w.step(3)
    w.place(T0 - 1010, T0 + 990)
    h = w.v.hook
    check(not h.gapSinceVerify and h.refCachedAt == w.block and h.refGrid == w.ts() // t.GRID_SEC and h.refTick == T0,
          "registration clears the gap and writes the live tick into the cache")


def rules_actions():
    t = R.TEST
    w = World(seed_amounts=(300_000 * USDC, 20 * ETH), third_L=10 ** 22)
    w.place(T0, T0 + 2000)  # token0-side edge: token1 stays idle
    w.deposit(10 ** 26)
    # deposit amounts round up
    S, i0, i1 = w.v.S, w.v.h.idle0, w.v.h.idle1
    sh = 10 ** 25 + 7
    w.deposit(sh)
    check(w.v.h.idle0 == i0 + R.ceil_div(i0 * sh, S) and w.v.h.idle1 == i1 + R.ceil_div(i1 * sh, S),
          "deposit idle slices round up")
    # NAV cap
    try:
        w.deposit(w.v.bank.bondShares * R.WAD // t.BOND_RATIO)
        check(False, "a deposit past the NAV cap must revert")
    except R.Revert as ex:
        check(ex.err == "NavCap", "NAV cap in shares", ex.err)
    # withdraw epilogue re-base
    n0, S0 = w.v.NAV0, w.v.S
    w.withdraw(10 ** 25)
    check(w.v.NAV0 == R.mul_div(n0, w.v.S, S0), "withdraw epilogue re-bases NAV0")
    # move to an epoch start so that the swap counters are fresh
    w.keepalive_run(t.EPOCH - w.block % t.EPOCH + 1)
    w.poke()
    nav = w.v.nav(sqrt_at(T0))
    # MAX_SWAP_ACTION (idle is sufficient, so the cap binds)
    try:
        w.v.rebalance_swap(False, nav * 25 // 1000, w.block, w.ts())
        check(False, "a swap above MAX_SWAP_ACTION must revert")
    except R.Revert as ex:
        check(ex.err == "SwapCap" and nav * 25 // 1000 <= w.v.h.idle1, "MAX_SWAP_ACTION", ex.err)
    # idle check
    try:
        w.v.rebalance_swap(True, w.v.h.idle0 + 1, w.block, w.ts())
        check(False, "a swap above idle must revert")
    except R.Revert as ex:
        check(ex.err == "SwapCap", "amountIn above idle reverts SwapCap", ex.err)
    # epoch cap: 1.9% + 1.9% then 1.9% exceeds 5%
    for i in range(2):
        w.v.rebalance_swap(False, nav * 19 // 1000, w.block, w.ts())
        w.keepalive_run(t.COOLDOWN)
    check(w.v.epochSwapFrac > 37 * 10 ** 15, "two 1.9% swaps filled", w.v.epochSwapFrac)
    try:
        w.v.rebalance_swap(False, nav * 19 // 1000, w.block, w.ts())
        check(False, "the epoch swap cap must revert")
    except R.Revert as ex:
        check(ex.err == "SwapCap", "MAX_SWAP_EPOCH", ex.err)
    # rebalanceSwap epilogue and collect-again with the vault's own position in range
    w2 = World()
    w2.place(T0 - 1000, T0 + 1000)
    w2.deposit(10 ** 26)
    w2.keepalive_run(t.COOLDOWN)
    w2.v.rebalance_swap(True, w2.v.h.idle0 // 2 + 1, w2.block, w2.ts())
    v = w2.v
    check(v.pool.collect("vault", v.h.lo, v.h.hi) == (0, 0), "fees collected again after the vault's own swap")
    check(v.NAV0 == v.nav(sqrt_at(T0)) and v.delta0 == R.delta_of(v.h, sqrt_at(T0)), "rebalanceSwap epilogue re-base")
    # rebalanceSwap fence
    v.pool.push_to(sqrt_at(T0 + 61))
    w2.keepalive_run(t.COOLDOWN)
    try:
        v.rebalance_swap(False, 10 ** 15, w2.block, w2.ts())
        check(False, "rebalanceSwap outside the fence must revert")
    except R.Revert as ex:
        check(ex.err == "Fence", "fence on rebalanceSwap", ex.err)
    # MAX_SWAPS and the counter reset at the epoch roll
    w3 = World(tier=R.Tier(**{**t.__dict__, "EPOCH": 600}), seed_amounts=(300_000 * USDC, 20 * ETH), third_L=10 ** 21)
    t = w3.t  # MAX_SWAPS cannot bind at TEST's EPOCH 120 with COOLDOWN 14 (8 actions per epoch)
    w3.place(T0, T0 + 2000)
    w3.keepalive_run(t.EPOCH - w3.block % t.EPOCH + 1)
    w3.poke()
    small = w3.v.nav(sqrt_at(T0)) // 1000
    for i in range(t.MAX_SWAPS):
        w3.v.rebalance_swap(False, small, w3.block, w3.ts())
        w3.keepalive_run(t.COOLDOWN)
    try:
        w3.v.rebalance_swap(False, small, w3.block, w3.ts())
        check(False, "MAX_SWAPS must revert")
    except R.Revert as ex:
        check(ex.err == "MaxSwaps", "MAX_SWAPS per epoch", ex.err)
    w3.keepalive_run(t.EPOCH - w3.block % t.EPOCH + 1)
    w3.poke()
    try:
        w3.v.rebalance_swap(False, small, w3.block, w3.ts())
        check(w3.v.swapCount == 1, "swap counters reset at the epoch roll")
    except R.Revert as ex:
        check(False, "swap counters reset at the epoch roll", ex.err)


def rules_r2():
    """Clause-level checks added after fix review r2 (each kills a named mutant in model/mutants_model.py)."""
    t = R.TEST
    k = 10 ** 18
    # Breach that does not exit keeps placedByCurator; a new forfeit restarts the drip with T = remaining + new
    b = R.Bank()
    b.dripBlocks, b.placedByCurator = 120, True
    b.cohorts[0] = [5, k]
    R.fee_bank_apply(b, "Breach", dict(count=1, now=500))
    check(b.placedByCurator and b.mode == R.BREACHED, "a non-exiting breach keeps placedByCurator")
    b = R.Bank()
    b.dripBlocks = 120
    b.forfeitPool = b.forfeitTotal = 12 * k
    b.forfeitStart = 1000
    b.cohorts[3] = [8, 2 * k]
    burned = R.fee_bank_apply(b, "Breach", dict(count=1, now=1060))[0]
    check(burned == 6 * k and b.forfeitPool == 8 * k and b.forfeitTotal == 8 * k and b.forfeitStart == 1060,
          "drip owed burned first, then T = remaining + new, start = now", (burned, b.forfeitPool, b.forfeitTotal))
    burned = R.fee_bank_apply(b, "Drip", dict(now=1061))[0]
    check(burned == 8 * k // 120, "restarted drip burns T/dripBlocks per block", burned)
    # Charge never burns vested pay
    b = R.Bank()
    b.cohorts[0], b.maturedPending, b.vestedUnclaimed, b.bondShares = [3, k], k, 5 * k, 10 * k
    R.fee_bank_apply(b, "Charge", dict(burn=4 * k))
    check(b.vestedUnclaimed == 5 * k and b.bondShares == 8 * k, "vested pay is never burned")
    # τ_v needs the cached reference in range; the span is credited with the pre-commit cache
    h = R.Hook(t, R.RefSource(lambda ts: T0 + 1500))
    h.armed, h.vaultLo, h.vaultHi, h.emptyRange = True, T0 - 1000, T0 + 1000, False
    h.refTick, h.refGrid, h.refCachedAt, h.lastSnap, h.ring = T0 + 1500, 10 ** 9, 1000, 1000, [(1000, T0)]
    h.snapshot(1020, 10 ** 12, sqrt_at(T0), T0)
    check(h.tauV == 0 and h.outBlocks == 20, "τ_v needs the cached reference in range")
    h = R.Hook(t, R.RefSource(lambda ts: T0 + 1500))
    h.armed, h.vaultLo, h.vaultHi, h.emptyRange = True, T0 - 1000, T0 + 1000, False
    h.refTick, h.refGrid, h.refCachedAt, h.lastSnap, h.ring = T0, 100, 2000, 2000, [(2000, T0)]
    h.snapshot(2020, 2020 * t.BLOCKTIME, sqrt_at(T0), T0)
    check(h.tauV == 20 and h.outBlocks == 0 and h.refTick == T0 + 1500,
          "the span is credited with the cache before this block's commit", (h.tauV, h.outBlocks))
    check(h.frozen.get(101) is not None and h.frozen[101][1] == 20, "frozen τ_v at the grid boundary", h.frozen)
    h = R.Hook(t, R.RefSource(lambda ts: T0))
    h.armed, h.vaultLo, h.vaultHi, h.emptyRange = True, T0 - 1000, T0 + 1000, False
    h.refTick, h.refGrid, h.refCachedAt, h.lastSnap, h.ring, h.med9 = T0, 10 ** 9, 1000, 1005, [(1005, T0)], T0
    h.snapshot(1035, 10 ** 12, sqrt_at(T0), T0)
    check(h.frozen.get(51) == (T0, 15, 0), "τ_v(B) interpolated at the boundary inside the span", h.frozen)
    # latch at exactly CURE; two pending latches keep the last block
    h = R.Hook(t, R.RefSource(lambda ts: T0 + 3000))
    h.armed, h.vaultLo, h.vaultHi, h.emptyRange = True, T0 - 1000, T0 + 1000, False
    h.refTick, h.refGrid, h.refCachedAt, h.lastSnap, h.outBlocks, h.ring = T0 + 3000, 10 ** 9, 1000, 1000, 160, [(1000, T0)]
    h.snapshot(1040, 10 ** 12, sqrt_at(T0), T0)
    check(h.pendingBreaches == 1 and h.lastPendingBlock == 1040, "latch when outBlocks reaches CURE exactly")
    h.outBlocks, h.refCachedAt = 390, 1040
    h.snapshot(1080, 10 ** 12, sqrt_at(T0), T0)
    check(h.pendingBreaches == 2 and h.lastPendingBlock == 1050, "the latest crossing is the pending cutoff",
          h.lastPendingBlock)
    # the fence is two-sided on rebalanceSwap; actions fail closed on an unreadable reference
    w = World()
    w.v.pool.push_to(sqrt_at(T0 - 61))
    w.keepalive_run(1)
    try:
        w.v.rebalance_swap(False, 10 ** 15, w.block, w.ts())
        check(False, "a swap with spot below the fence must revert")
    except R.Revert as ex:
        check(ex.err == "Fence", "two-sided fence", ex.err)
    w.v.pool.push_to(sqrt_at(T0))
    w.keepalive_run(1)
    w.ref.fail = lambda end: end == w.ts()
    try:
        w.v.rebalance_swap(False, 10 ** 15, w.block, w.ts())
        check(False, "an unreadable reference must revert the action")
    except R.Revert as ex:
        check(ex.err == "RefUnreadable", "actions fail closed on an unreadable reference", ex.err)
    w.ref.fail = set()
    # swap cap valued at the live reference on the current NAV; limit on the adverse side; counters add the fill
    st = {"ref": T0}
    w = World(ref_fn=lambda ts: st["ref"], third_L=10 ** 22)
    st["ref"] = T0 + 200
    w.v.pool.push_to(sqrt_at(T0 + 200))
    w.keepalive_run(3 * t.GRID)
    s_ref = sqrt_at(T0 + 200)
    nav = w.v.nav(s_ref)
    amt = R.mul_div(nav * 1995 // 100000, 1 << 192, s_ref * s_ref)
    try:
        w.v.rebalance_swap(True, amt, w.block, w.ts())
        frac = w.v.epochSwapFrac
        check(True, "1.995% at the live reference passes the per-action cap")
    except R.Revert as ex:
        check(False, "1.995% at the live reference passes the per-action cap", ex.err)
        frac = 0
    w.keepalive_run(t.COOLDOWN)
    w.v.pool.push_to(sqrt_at(T0 + 150))
    s_ref = sqrt_at(T0 + 200)
    nav = w.v.nav(s_ref)
    amt = R.mul_div(nav * 2003 // 100000, 1 << 192, s_ref * s_ref)
    try:
        w.v.rebalance_swap(True, amt, w.block, w.ts())
        check(False, "2.003% valued at the reference must revert even when spot is lower")
    except R.Revert as ex:
        check(ex.err in ("SwapCap", "Fence"), "swap cap valued at the reference", ex.err)
    # thin pool: a partial fill stops at the adverse limit and the counter adds the filled amount
    w = World(third_L=10 ** 14)
    s_ref = sqrt_at(T0)
    amt = min(w.v.h.idle0, R.mul_div(w.v.nav(s_ref) * 19 // 1000, 1 << 192, s_ref * s_ref))
    i0 = w.v.h.idle0
    nav_before = w.v.nav(s_ref)
    w.v.rebalance_swap(True, amt, w.block, w.ts())
    used = i0 - w.v.h.idle0
    nav_ref = w.v.nav(s_ref)
    check(w.v.epochSwapFrac == R.mul_div_up(R.q(used, s_ref), R.WAD, nav_before),
          "epoch counter = ceil(filled value · WAD / NAV(ref))", (w.v.epochSwapFrac, used))
    check(used < amt and w.v.pool.tick == T0 - 10 - 1 or w.v.pool.s == sqrt_at(T0 - 10),
          "a swap stops at spot − (FEE_TICKS + BAND_SWAP)", (w.v.pool.tick, used, amt))
    check(w.v.pool.s >= sqrt_at(T0 - 10), "the limit is on the adverse side and is never passed", w.v.pool.s)
    check(w.v.epochSwapFrac < R.mul_div_up(R.q(amt, s_ref), R.WAD, nav_ref) and w.v.epochSwapFrac > 0,
          "epoch counter adds the filled amount", w.v.epochSwapFrac)
    # the per-action fraction rounds up: a request whose floor fraction equals the cap reverts
    w = World(seed_amounts=(150_000 * USDC, 500 * ETH), third_L=10 ** 22)
    nav = w.v.nav(sqrt_at(T0))
    amt = nav * t.MAX_SWAP_ACTION // R.WAD + 1
    check(amt * R.WAD // nav == t.MAX_SWAP_ACTION and amt <= w.v.h.idle1, "boundary request constructed")
    try:
        w.v.rebalance_swap(False, amt, w.block, w.ts())
        check(False, "a request just above the cap must revert")
    except R.Revert as ex:
        check(ex.err == "SwapCap", "per-action fraction rounds up", ex.err)
    # DELTA_MIN boundary in the vault: deltaAfter == DELTA_MIN places, one raw unit less reverts
    for x, ok in ((1496491847102831049, True), (1496491847102831048, False)):
        st = {"ref": T0 + 30}
        w = World(ref_fn=lambda ts: st["ref"], third_L=0, seed_amounts=(x, 5 * 10 ** 26))
        w.v.pool.push_to(sqrt_at(T0) + 1)
        w.step(1)
        try:
            ch, p, burn, d = w.place(T0, T0 + 2000)
            check(ok and d == t.DELTA_MIN, "deltaAfter == DELTA_MIN places", d)
        except R.Revert as ex:
            check(not ok and ex.err == "DeltaMin", "deltaAfter one unit below DELTA_MIN reverts", ex.err)
    # a pre-swap counts toward the caps and the counters; it is charged with the placement (charge from the
    # holdings before the pre-swap)
    w = World(seed_amounts=(300_000 * USDC, 20 * ETH), third_L=10 ** 21)
    w.place(T0, T0 + 2000)
    w.keepalive_run(t.COOLDOWN)
    w.v.pool.push_to(sqrt_at(T0 + 55))
    w.keepalive_run(1)
    w.poke()  # sweep the fees the push paid to the position, so `before` matches the action's collect-first state
    before = w.v.h.copy()
    s_ref = sqrt_at(T0)
    s_spot = w.v.pool.s
    pre = dict(active=True, zeroForOne=False, amountIn=w.v.nav(s_ref) // 100)
    n0 = w.v.swapCount
    ch, p, burn, d = w.place(T0, T0 + 2000, pre)
    expect = max(0, R.value(before, s_spot)[0] - R.value(w.v.h, s_spot)[0], R.value(before, s_ref)[0] - R.value(w.v.h, s_ref)[0])
    check(w.v.swapCount == n0 + 1 and w.v.epochSwapFrac > 0, "a pre-swap counts toward MAX_SWAPS and the epoch cap")
    check(ch == expect, "the pre-swap is charged with the placement", (ch, expect))
    check(w.v.NAV0 == w.v.nav(s_ref) and w.v.delta0 == R.delta_of(w.v.h, s_ref),
          "setPositions epilogue: NAV0 and δ0 at the live reference, not at spot")
    # rebalanceSwap never changes the mode and never resets the clock
    st = {"ref": T0}
    w = World(ref_fn=lambda ts: st["ref"], third_L=10 ** 21)
    w.place(T0 - 1000, T0 + 1000)
    w.keepalive_run(t.COOLDOWN)
    w.v.bank.mode = R.BREACHED
    st["ref"] = T0 + 1010
    w.v.pool.push_to(sqrt_at(T0 + 1010))
    w.keepalive_run(3 * t.GRID)
    ob = w.v.hook.outBlocks
    w.v.rebalance_swap(False, w.v.h.idle1 // 10 + 1, w.block, w.ts())
    check(w.v.bank.mode == R.BREACHED and w.v.hook.outBlocks == ob, "rebalanceSwap changes neither the mode nor the clock")
    # two pending breaches are both counted
    w = World()
    w.place(T0 - 1000, T0 + 1000)
    w.keepalive_run(40)
    h = w.v.hook
    h.pendingBreaches, h.lastPendingBlock, h.tauVAtLastPending = 2, w.block, h.tauV
    w.step(1)
    w.poke()
    check(w.v.bank.breaches == 2 and h.pendingBreaches == 0, "two pending breaches are both processed")
    # deposits checkpoint first (base minted before the deposit)
    w = World()
    w.place(T0 - 1000, T0 + 1000)
    w.keepalive_run(60)
    c0 = w.v.bank.cohort_total()
    w.deposit(10 ** 24)
    check(w.v.bank.cohort_total() > c0, "a deposit runs the checkpoint first")
    # withdraw pays floor(bucket·s / S_pre)
    w = World()
    w.place(T0 - 1000, T0 + 1000)
    w.deposit(10 ** 26)
    S, i0, i1, L = w.v.S, w.v.h.idle0, w.v.h.idle1, w.v.h.L
    sh = 10 ** 25 + 7
    w.withdraw(sh)
    check(w.v.h.idle0 == i0 - i0 * sh // S and w.v.h.idle1 == i1 - i1 * sh // S and w.v.h.L == L - L * sh // S,
          "withdraw pays floor(bucket·s/S) with the pre-burn supply")
    # a matured cohort with a pending breach and the gate open is forfeited, not vested
    w = World()
    w.place(T0 - 1000, T0 + 1000)
    w.keepalive_run(t.EPOCH)
    w.poke()
    w.keepalive_run(t.EPOCH * (1 + R.V))
    h = w.v.hook
    h.pendingBreaches, h.lastPendingBlock, h.tauVAtLastPending = 1, w.block, h.tauV
    w.step(1)
    vu = w.v.bank.vestedUnclaimed
    w.poke()
    check(w.v.bank.vestedUnclaimed == vu and w.v.bank.unvested() <= w.v.bank.cohort_total(),
          "breaches are processed before vesting")
    # the gate needs a fresh cache: at exactly refCachedAt + 2·GRID (stale, no gap yet) nothing vests
    w = World()
    w.place(T0 - 1000, T0 + 1000)
    w.keepalive_run(t.EPOCH)
    w.poke()
    w.keepalive_run(t.EPOCH * (1 + R.V))
    w.step(t.GRID - w.block % t.GRID + 3)  # first touch of a new anchor, 3 blocks after it: the cache is unaligned
    w.touch()
    cached = w.v.hook.refCachedAt
    check(cached == w.block, "refresh at the first touch of the anchor")
    w.ref.fail = lambda end: end % t.GRID_SEC == 0  # anchored reads fail; live reads (unaligned) succeed
    w.block = cached + 2 * t.GRID
    vu = w.v.bank.vestedUnclaimed
    w.poke()
    check(not w.v.hook.gapSinceVerify and w.v.bank.vestedUnclaimed == vu and w.v.bank.maturedPending > 0,
          "a stale cache closes the gate", (w.v.hook.gapSinceVerify, w.v.bank.vestedUnclaimed - vu))
    w.ref.fail = set()
    # the forfeit drips: half an epoch after a breach about half the pool is burned
    w = World()
    w.place(T0 - 1000, T0 + 1000)
    w.keepalive_run(t.EPOCH)
    w.poke()
    h = w.v.hook
    h.pendingBreaches, h.lastPendingBlock, h.tauVAtLastPending = 1, w.block, h.tauV
    w.step(1)
    w.poke()
    T = w.v.bank.forfeitTotal
    w.keepalive_run(t.EPOCH // 2)
    w.poke()
    check(T > 0 and abs(w.v.bank.forfeitPool - T // 2) <= T // 20, "forfeit drips over one epoch", (w.v.bank.forfeitPool, T))
    # registration writes the live tick (not the old cache)
    st = {"ref": T0}
    w = World(ref_fn=lambda ts: st["ref"])
    w.place(T0 - 1000, T0 + 1000)
    w.keepalive_run(t.COOLDOWN)
    st["ref"] = T0 + 30
    w.step(1)
    w.place(T0 - 1000, T0 + 1000)
    check(w.v.hook.refTick == T0 + 30, "registration writes the live tick", w.v.hook.refTick)
    # seed: an unreadable reference reverts; BOND_MIN is checked at REF_SQRT_PRICE, not at the pool price
    try:
        World(ref_fail=lambda end: True)
        check(False, "seed with an unreadable reference must revert")
    except R.Revert as ex:
        check(ex.err == "RefUnreadable", "seed requires a successful refresh", ex.err)
    probe = World()
    bond_val = R.mul_div(probe.v.nav(sqrt_at(T0)), probe.v.bank.bondShares, probe.v.S)
    tier = R.Tier(**{**t.__dict__, "BOND_MIN": bond_val})
    w = World.__new__(World)
    w.t, w.ref_tick, w.fail = tier, (lambda ts: T0), set()
    w.ref = R.RefSource(lambda ts: T0, w.fail)
    pool = R.Pool(sqrt_at(T0 - 500), tier.FEE)
    w.v = R.Vault(tier, w.ref, pool, sqrt_ref_price=sqrt_at(T0))
    w.block, w.depositor = 12_000, 0
    try:
        w.v.seed(150_000 * USDC, 50 * ETH, now=w.block, ts=w.ts())
        check(True, "BOND_MIN valued at REF_SQRT_PRICE")
    except R.Revert as ex:
        check(False, "BOND_MIN valued at REF_SQRT_PRICE", ex.err)
    tier2 = R.Tier(**{**t.__dict__, "BOND_MIN": bond_val + 10 ** 12})
    w.v = R.Vault(tier2, w.ref, R.Pool(sqrt_at(T0), tier2.FEE), sqrt_ref_price=sqrt_at(T0))
    try:
        w.v.seed(150_000 * USDC, 50 * ETH, now=w.block, ts=w.ts())
        check(False, "a bond below BOND_MIN must revert")
    except R.Revert as ex:
        check(ex.err == "BondMin", "BOND_MIN enforced at seed", ex.err)
    # prologue collect-first sweeps fees earned between actions
    w = World(third_L=10 ** 17)
    w.place(T0 - 1000, T0 + 1000)
    w.v.pool.swap(True, 10 ** 10, sqrt_at(T0 - 5))
    w.v.pool.swap(False, 10 ** 17, sqrt_at(T0 + 5))
    w.keepalive_run(1)
    w.poke()
    check(w.v.pool.collect("vault", w.v.h.lo, w.v.h.hi) == (0, 0), "collect-first sweeps fees in every prologue")


def rules_r3():
    """Checks added after fix review r3: segment close, mint price, hook range edges, timestamp phase, inclusive
    guard boundaries, epoch-roll records."""
    t = R.TEST
    # two consecutive checkpoints each mint exactly the base of their own segment (τ_v0 := τ_v closes it)
    w = World()
    w.place(T0 - 1000, T0 + 1000)
    w.deposit(10 ** 26)
    for i in range(2):
        w.keepalive_run(100)
        v = w.v
        S0, tau0, nav0, d0 = v.S, v.tauV0, v.NAV0, v.delta0
        w.step(1)
        o = v.hook.snapshot(w.block, w.ts(), v.pool.s, v.pool.tick)
        base = R.mul_div(R.mul_div(t.m_perBlock, nav0, R.WAD), d0 * (o["tauV"] - tau0), R.WAD)
        v._collect_first()
        navp = v.nav(sqrt_at(v.hook.refTick))
        v._checkpoint(o, w.block, w.ts())
        check(o["tauV"] - tau0 > 90 and v.S - S0 == base * S0 // (navp - base),
              f"checkpoint {i + 1} mints exactly its own segment's base", (v.S - S0, base))
        check(v.tauV0 == o["tauV"], "the checkpoint closes the segment (τ_v0 := τ_v)")
    # mint price = the fresh cache, not spot (spot pushed inside the fence)
    w = World()
    w.place(T0 - 1000, T0 + 1000)
    w.deposit(10 ** 26)
    w.keepalive_run(60)
    w.v.pool.push_to(sqrt_at(T0 + 55))
    w.step(1)
    v = w.v
    S0, tau0, m0 = v.S, v.tauV0, v.mint_reads
    o = v.hook.snapshot(w.block, w.ts(), v.pool.s, v.pool.tick)
    base = R.mul_div(R.mul_div(t.m_perBlock, v.NAV0, R.WAD), v.delta0 * (o["tauV"] - tau0), R.WAD)
    v._collect_first()
    navp = v.nav(sqrt_at(T0))
    v._checkpoint(o, w.block, w.ts())
    check(o["refFresh"] and v.mint_reads == m0 and v.S - S0 == base * S0 // (navp - base),
          "base shares priced at the fresh cache, not at spot", (v.S - S0, base * S0 // (navp - base)))
    # freshness boundary: at refCachedAt + 2·GRID − 1 the cache is still fresh (no tryRef for the mint)
    w = World()
    w.place(T0 - 1000, T0 + 1000)
    w.keepalive_run(30)
    w.step(t.GRID - w.block % t.GRID + 3)
    w.touch()
    cached = w.v.hook.refCachedAt
    w.ref.fail = lambda end: end % t.GRID_SEC == 0  # no new anchor sample
    w.block = cached + 2 * t.GRID - 1
    m0 = w.v.mint_reads
    w.poke()
    check(w.v.mint_reads == m0 and w.v.hook.fresh(w.block), "fresh while now < refCachedAt + 2·GRID", w.v.mint_reads - m0)
    w.ref.fail = set()
    # hook range edges: the clock counts a reference parked at hi and not one parked at lo
    for tick, out in ((T0 + 1000, True), (T0 - 1000, False)):
        w = World(ref_fn=lambda ts, tick=tick: tick)
        w.v.pool.push_to(sqrt_at(T0))
        h = w.v.hook
        h.vaultLo, h.vaultHi, h.emptyRange = T0 - 1000, T0 + 1000, False
        w.keepalive_run(t.CURE_BLOCKS + 3 * t.GRID)
        check((h.pendingBreaches > 0) == out and (h.outBlocks > 0 or h.pendingBreaches > 0) == out,
              "in range is lo <= tick < hi for the cached reference", (tick - T0, h.outBlocks, h.pendingBreaches))
    # timestamp phase: anchors follow ts, not the block grid
    for phase in (150, 239):
        w = World(phase=phase)
        w.place(T0 - 1000, T0 + 1000)
        for _ in range(12):
            w.step(7)
            w.touch()
            check(w.v.hook.refGrid == w.ts() // t.GRID_SEC, "the anchor index is ts / GRID_SEC", (phase, w.block))
    # inclusive guard boundaries
    tb = R.Tier(**{**t.__dict__, "BOND_RATIO": 6 * 10 ** 14})   # a ratio that divides bond·WAD, so the cap is hit exactly
    w = World(tier=tb, seed_amounts=(150_000 * USDC, 500 * ETH), third_L=10 ** 22)
    cap_shares = w.v.bank.bondShares * R.WAD // tb.BOND_RATIO - w.v.S
    try:
        w.deposit(cap_shares)
        check(w.v.S * tb.BOND_RATIO == w.v.bank.bondShares * R.WAD, "deposit to exactly the NAV cap passes")
    except R.Revert as ex:
        check(False, "deposit to exactly the NAV cap passes", ex.err)
    w = World(seed_amounts=(150_000 * USDC, 500 * ETH), third_L=10 ** 22)
    nav = w.v.nav(sqrt_at(T0))
    exact = None
    for a in range(nav * t.MAX_SWAP_ACTION // R.WAD - 3, nav * t.MAX_SWAP_ACTION // R.WAD + 4):
        if R.mul_div_up(a, R.WAD, nav) == t.MAX_SWAP_ACTION:
            exact = a
    try:
        w.v.rebalance_swap(False, exact, w.block, w.ts())
        check(True, "a fraction exactly MAX_SWAP_ACTION passes")
    except R.Revert as ex:
        check(False, "a fraction exactly MAX_SWAP_ACTION passes", ex.err)
    # epoch total exactly MAX_SWAP_EPOCH passes
    w.keepalive_run(t.COOLDOWN)
    nav = w.v.nav(sqrt_at(T0))
    left = t.MAX_SWAP_EPOCH - w.v.epochSwapFrac
    a2 = None
    for a in range(nav * min(left, t.MAX_SWAP_ACTION) // R.WAD - 3, nav * min(left, t.MAX_SWAP_ACTION) // R.WAD + 4):
        if R.mul_div_up(a, R.WAD, nav) == min(left, t.MAX_SWAP_ACTION):
            a2 = a
    w.v.rebalance_swap(False, a2, w.block, w.ts())
    w.keepalive_run(t.COOLDOWN)
    nav = w.v.nav(sqrt_at(T0))
    left = t.MAX_SWAP_EPOCH - w.v.epochSwapFrac
    a3 = max(a for a in range(nav * left // R.WAD - 3, nav * left // R.WAD + 4) if R.mul_div_up(a, R.WAD, nav) <= left)
    ep = w.block // t.EPOCH
    try:
        w.v.rebalance_swap(False, a3, w.block, w.ts())
        check(w.block // t.EPOCH == ep, "same epoch")
        check(True, "an epoch total of exactly MAX_SWAP_EPOCH passes")
    except R.Revert as ex:
        check(False, "an epoch total of exactly MAX_SWAP_EPOCH passes", ex.err)
    # amountIn equal to idle passes
    w = World(seed_amounts=(300_000 * USDC, 1 * ETH), third_L=10 ** 22)
    try:
        w.v.rebalance_swap(False, w.v.h.idle1, w.block, w.ts())
        check(True, "amountIn == idle passes")
    except R.Revert as ex:
        check(False, "amountIn == idle passes", ex.err)
    # the live leg is not read at nowEpoch = e + V (not yet vestable)
    w = World()
    w.place(T0 - 1000, T0 + 1000)
    w.keepalive_run(t.EPOCH - w.block % t.EPOCH + 1)
    w.keepalive_run(50)
    w.poke()
    e = min(c[0] for c in w.v.bank.cohorts if c[1] > 0)
    w.keepalive_run((e + R.V) * t.EPOCH + 5 - w.block)
    r0 = w.v.live_reads
    w.poke()
    check(w.block // t.EPOCH == e + R.V and w.v.live_reads == r0, "no live read at nowEpoch = e + V", w.v.live_reads - r0)
    # deposit adds ceilDiv(L·s, S) liquidity
    w = World()
    w.place(T0 - 1000, T0 + 1000)
    w.settle_due()
    S, L = w.v.S, w.v.h.L
    sh = 10 ** 21 + 7
    w.deposit(sh)
    check(w.v.h.L == L + R.ceil_div(L * sh, S), "deposit adds ceilDiv(L·s, S)", (w.v.h.L - L, R.ceil_div(L * sh, S)))
    # sizing with spot exactly at sqrtAt(lo) (all token0) and at sqrtAt(hi) (all token1)
    for sp, idle, side in ((sqrt_at(T0), R.Holdings(10 ** 12, 0), 0), (sqrt_at(T0 + 2000), R.Holdings(0, 10 ** 21), 1)):
        try:
            L, after, d = R.placement_size(idle, T0, T0 + 2000, sp, sqrt_at(T0 + 1000))
            check(L > 0 and (after.idle0 < idle.idle0 if side == 0 else after.idle1 < idle.idle1),
                  "sizing at exactly a range edge", (side, L))
        except Exception as ex:
            check(False, "sizing at exactly a range edge", repr(ex))
    # nothing to mint: no tryRef read for the mint price
    w = World()
    w.ref.fail = lambda end: end % t.GRID_SEC == 0  # the cache cannot refresh, so it is stale at the poke
    w.step(10 * t.GRID + 3)
    m0 = w.v.mint_reads
    w.poke()
    check(not w.v.hook.fresh(w.block), "stale cache at the poke")
    w.ref.fail = set()
    check(w.v.mint_reads == m0, "no mint-price read when there is no base to mint", w.v.mint_reads - m0)
    # a placement that sizes to L = 0 reverts DeltaMin (and never touches an empty position)
    w = World(seed_amounts=(0, 50 * ETH))
    w.v.pool.push_to(sqrt_at(T0) + 1)
    w.step(1)
    try:
        w.place(T0, T0 + 2000)
        check(False, "an L = 0 placement must revert")
    except R.Revert as ex:
        check(ex.err == "DeltaMin", "an L = 0 placement reverts DeltaMin", ex.err)
    # an unplaced vault's prologue never modifies an empty position
    w = World()
    w.keepalive_run(3)
    w.poke()
    check(w.v.h.L == 0, "unplaced prologue ok")
    # claim pays vested pay only (never maturedPending or cohorts)
    b = R.Bank()
    b.vestedUnclaimed, b.maturedPending = 3, 5
    b.cohorts[0] = [0, 7]
    x = R.fee_bank_apply(b, "Claim", {})[1]
    check(x == 3 and b.maturedPending == 5 and b.cohorts[0][1] == 7, "claim pays vestedUnclaimed only")
    # epoch roll records: the previous epoch's end and one stale record per skipped gap
    w = World()
    w.place(T0 - 1000, T0 + 1000)
    e0 = w.v.epoch
    w.step(3 * t.EPOCH)
    w.poke()
    check("end" in w.v.records.get(e0, {}) and "stale" in w.v.records.get(e0 + 1, {}).get("flags", set()),
          "epoch roll writes the end and flags the gap record stale")
    w2 = World()
    w2.place(T0 - 1000, T0 + 1000)
    e1 = w2.v.epoch
    w2.keepalive_run(t.EPOCH - w2.block % t.EPOCH + 1)
    w2.poke()
    check("stale" not in w2.v.records.get(e1 + 1, {}).get("flags", set()), "no stale record when no epoch is skipped")


def rules_r4():
    """Checks added after fix review r4: exact deferred and BREACHED base, the spot leg of the charge, every entry
    point's prologue, fee credit, swap output, records, and the remaining guards."""
    import copy as _copy
    t = R.TEST

    def exact_checkpoint(w, label):
        """Run one checkpoint by hand and assert the exact minted shares or the exact deferral (step 4)."""
        v = w.v
        w.step(1)
        o = v.hook.snapshot(w.block, w.ts(), v.pool.s, v.pool.tick)
        v._collect_first()
        elig = v.bank.placedByCurator and v.bank.mode in (R.NORMAL, R.BREACHED)
        dtau = o["tauV"] - v.tauV0
        base = R.mul_div(R.mul_div(t.m_perBlock, v.NAV0, R.WAD), v.delta0 * dtau, R.WAD) if (elig and dtau > 0) else 0
        amount = base + v.baseOwed
        price = o["refTick"] if o["refFresh"] else None
        if price is None:
            ok, tk = w.ref.read(w.ts(), 0)
            price = tk if ok else None
        S0 = v.S - R._drip_now(_copy.deepcopy(v.bank), w.block)  # the drip owed at step (2) burns first
        nav = v.nav(sqrt_at(price)) if price is not None else 0
        v._checkpoint(o, w.block, w.ts())
        if amount == 0:
            check(v.S == S0 and v.baseOwed == 0, f"{label}: nothing to mint")
        elif price is None or nav <= amount:
            check(v.S == S0 and v.baseOwed == amount, f"{label}: deferred exactly", (v.baseOwed, amount))
        else:
            sh = amount * S0 // (nav - amount)
            check(v.S - S0 == sh and v.baseOwed == 0, f"{label}: minted exactly", (v.S - S0, sh))
        return base, amount, price
    # two consecutive unpriced checkpoints accumulate baseOwed; the priced one mints base + baseOwed
    w = World()
    w.place(T0 - 1000, T0 + 1000)
    w.deposit(10 ** 26)
    w.keepalive_run(100)
    w.ref.fail = lambda end: True
    w.step(60)
    b1, a1, _ = exact_checkpoint(w, "unpriced checkpoint 1")
    w.step(5)
    b2, a2, _ = exact_checkpoint(w, "unpriced checkpoint 2")
    check(b1 > 0 and a2 == a1 + b2, "deferrals accumulate", (a1, b2, a2))
    w.ref.fail = set()
    w.touch()
    b3, a3, p3 = exact_checkpoint(w, "priced checkpoint after two deferrals")
    check(p3 is not None and a3 >= a2, "the priced checkpoint mints the deferred base too", (a3, a2))
    # stale cache but a readable live reference: mint at the tryRef price
    w = World()
    w.place(T0 - 1000, T0 + 1000)
    w.deposit(10 ** 26)
    w.keepalive_run(100)
    w.ref.fail = lambda end: end % t.GRID_SEC == 0  # anchors fail, so the cache goes stale; live reads work
    w.step(60)
    m0 = w.v.mint_reads
    b, a, p = exact_checkpoint(w, "stale cache, live price")
    check(a > 0 and p is not None and w.v.mint_reads == m0 + 1, "stale cache: mint at the tryRef price")
    w.ref.fail = set()
    # base accrues in BREACHED
    st = {"ref": T0}
    w = World(ref_fn=lambda ts: st["ref"])
    w.place(T0 - 1000, T0 + 1000)
    w.deposit(10 ** 26)
    st["ref"] = T0 + 1500
    w.keepalive_run(t.CURE_BLOCKS + 3 * t.GRID)
    w.poke()
    check(w.v.bank.mode == R.BREACHED, "breached")
    st["ref"] = T0
    w.keepalive_run(100)
    b, a, p = exact_checkpoint(w, "base in BREACHED")
    check(b > 0, "base accrues in BREACHED", b)
    # NAV' <= base defers (far reference)
    w = World(seed_amounts=(300_000 * USDC, 50 * ETH))
    w.place(T0 - 1000, T0 + 1000)
    w.keepalive_run(100)
    h = w.v.hook
    w.step(1)
    w.touch()
    h.refTick = T0 - 300000
    b, a, p = exact_checkpoint(w, "NAV' <= base")
    check(a > 0 and w.v.baseOwed == a, "NAV' <= base keeps the amount in baseOwed", (a, w.v.baseOwed))
    # a withdrawal works on an unplaced vault (L = 0)
    w = World()
    w.deposit(10 ** 24)
    w.step(3)
    out = w.withdraw(10 ** 24)
    check(out[0] > 0 and out[1] > 0, "withdraw from an unplaced vault (L = 0)")
    # the charge's spot leg is the pre-action spot (pStar = spot), for a swap and a placement with a pre-swap
    s_ref = sqrt_at(T0)
    w = World(seed_amounts=(150_000 * USDC, 500 * ETH), third_L=10 ** 19)
    w.v.pool.push_to(sqrt_at(T0 - 55))
    w.step(1)
    w.poke()
    before, s_pre = w.v.h.copy(), w.v.pool.s
    amt = w.v.nav(s_ref) * 15 // 1000
    c, p, burn = w.v.rebalance_swap(False, amt, w.block, w.ts())
    exp = R.measure(before, w.v.h, s_pre, s_ref, 1, 0, 0)
    check(w.v.pool.s != s_pre and p == s_pre and c == exp[0], "swap charge priced at the pre-action spot", (c, exp[0]))
    w = World(seed_amounts=(300_000 * USDC, 20 * ETH), third_L=10 ** 19)
    w.place(T0, T0 + 2000)
    w.keepalive_run(t.COOLDOWN)
    w.v.pool.push_to(sqrt_at(T0 + 40))
    w.step(1)
    w.poke()
    before, s_pre = w.v.h.copy(), w.v.pool.s
    pre = dict(active=True, zeroForOne=True, amountIn=w.v.h.idle0 // 50)
    c, p, burn, d = w.place(T0, T0 + 2000, pre)
    hh = w.v.h
    x, y = R.get_amount0_delta(w.v.pool.s, sqrt_at(hh.hi), hh.L, True), R.get_amount1_delta(sqrt_at(hh.lo), w.v.pool.s, hh.L, True)
    L_exp = R.placement_size(R.Holdings(hh.idle0 + x, hh.idle1 + y), hh.lo, hh.hi, w.v.pool.s, s_ref)[0]
    check(hh.L == L_exp and hh.idle0 >= 0 and hh.idle1 >= 0, "L sized at the post-pre-swap spot", (hh.L, L_exp))
    exp = R.measure(before, w.v.h, s_pre, s_ref, 1, 0, 0)
    check(w.v.pool.s != s_pre and c == exp[0] and p == exp[1], "placement charge priced at the pre-action spot",
          (c, exp[0], p == s_pre))
    # cooldown after rebalanceSwap
    w = World(seed_amounts=(150_000 * USDC, 500 * ETH), third_L=10 ** 22)
    w.v.rebalance_swap(False, 10 ** 17, w.block, w.ts())
    w.step(1)
    try:
        w.v.rebalance_swap(False, 10 ** 17, w.block, w.ts())
        check(False, "a second swap within COOLDOWN must revert")
    except R.Revert as ex:
        check(ex.err == "Cooldown", "COOLDOWN after rebalanceSwap", ex.err)
    # swap output reaches idle exactly (replay the same swap on a clone of the pool)
    w = World(seed_amounts=(150_000 * USDC, 500 * ETH), third_L=10 ** 20)
    for zfo in (True, False):
        w.keepalive_run(t.COOLDOWN)
        clone = _copy.deepcopy(w.v.pool)
        amt = (w.v.h.idle0 if zfo else w.v.h.idle1) // 100
        i0, i1 = w.v.h.idle0, w.v.h.idle1
        lim = sqrt_at(w.v.pool.tick - 10) if zfo else sqrt_at(w.v.pool.tick + 10)
        used, out = clone.swap(zfo, amt, lim)
        w.v.rebalance_swap(zfo, amt, w.block, w.ts())
        d0, d1 = w.v.h.idle0 - i0, w.v.h.idle1 - i1
        check((d0, d1) == ((-used, out) if zfo else (out, -used)), "swap moves idle by exactly the fill", (zfo, d0, d1))
    # collect-first credits the fees to idle
    w = World(third_L=10 ** 17)
    w.place(T0 - 1000, T0 + 1000)
    w.v.pool.swap(False, 10 ** 18, sqrt_at(T0 + 8))
    w.v.pool.swap(True, 10 ** 10, sqrt_at(T0 - 8))
    owed = _copy.deepcopy(w.v.pool).collect("vault", w.v.h.lo, w.v.h.hi)
    i0, i1 = w.v.h.idle0, w.v.h.idle1
    w.step(1)
    w.poke()
    check(owed[0] + owed[1] > 0 and (w.v.h.idle0 - i0, w.v.h.idle1 - i1) == owed, "collected fees credited to idle", owed)
    # every entry point runs the prologue (snapshot and checkpoint) before its own effect
    w = World()
    w.place(T0 - 1000, T0 + 1000)
    w.deposit(10 ** 25)
    for name, call in (("withdraw", lambda: w.withdraw(10 ** 20)), ("claim", lambda: w.claim()),
                       ("topUpBond", lambda: w.v.top_up_bond(5, w.block, w.ts())),
                       ("processBreach", lambda: w.v.process_breach(w.block, w.ts())),
                       ("poke", lambda: w.poke()), ("deposit", lambda: w.deposit(10 ** 20))):
        w.keepalive_run(30)
        w.step(1)
        bond0 = w.v.bank.bondShares
        call()
        check(w.v.hook.lastSnap == w.block and w.v.tauV0 == w.v.hook.tauV, f"{name} runs the prologue first")
        if name == "topUpBond":
            check(w.v.bank.bondShares == bond0 + 5, "topUpBond adds to the bond")
    # claim pays pay that its own prologue just vested
    w = World()
    w.place(T0 - 1000, T0 + 1000)
    w.keepalive_run(t.EPOCH)
    w.poke()
    w.keepalive_run(t.EPOCH * (1 + R.V))
    w.step(1)
    paid = w.claim()
    check(paid > 0, "claim's prologue vests matured pay before paying", paid)
    # releaseBond runs the prologue (the drip burns at its block)
    w = World()
    w.place(T0 - 1000, T0 + 1000)
    w.keepalive_run(t.EPOCH)
    w.poke()
    w.keepalive_run(t.COOLDOWN)
    w.v.retire(w.block, w.ts())
    w.keepalive_run(R.V * t.EPOCH)
    w.step(1)
    w.release()
    check(w.v.hook.lastSnap == w.block, "releaseBond runs the prologue")
    # epoch swap fraction resets at the roll
    w = World(seed_amounts=(150_000 * USDC, 500 * ETH), third_L=10 ** 22)
    w.v.rebalance_swap(False, 10 ** 17, w.block, w.ts())
    check(w.v.epochSwapFrac > 0, "fraction counted")
    w.keepalive_run(t.EPOCH)
    w.poke()
    check(w.v.epochSwapFrac == 0 and w.v.swapCount == 0, "epoch roll resets the swap fraction and count")
    # records: base, minted, charge, breach flag, forfeits, unverified, retire forfeitValue
    st = {"ref": T0}
    w = World(ref_fn=lambda ts: st["ref"])
    w.place(T0 - 1000, T0 + 1000)
    w.deposit(10 ** 26)
    w.keepalive_run(60)
    w.step(1)
    e = w.block // t.EPOCH
    r0 = dict(w.v.rec(e))
    S0 = w.v.S
    w.poke()
    r = w.v.rec(e)
    check(r["base"] > r0["base"] and r["minted"] - r0["minted"] == w.v.S - S0, "records: base and minted")
    w.keepalive_run(t.COOLDOWN)
    w.v.pool.push_to(sqrt_at(T0 + 50))
    w.step(1)
    ch0 = w.v.rec(w.v.epoch)["charge"]
    c, p, burn, d = w.place(T0 + 50 - 1990, T0 + 60)
    check(c > 0 and w.v.rec(w.v.epoch)["charge"] == ch0 + c, "records: charge", c)
    w.settle_due()  # the honest curator settles the markout
    st["ref"] = T0 + 2500
    w.keepalive_run(t.CURE_BLOCKS + 3 * t.GRID)
    unv = w.v.bank.unvested()
    e = w.v.epoch
    f0 = w.v.rec(e)["forfeited"]
    w.step(1)
    w.poke()
    rr = w.v.rec(e)
    check("breach" in rr["flags"] and rr["forfeited"] - f0 == unv and rr["forfeitValue"] > 0,
          "records: breach flag and forfeits", (rr["forfeited"] - f0, unv, rr["forfeitValue"]))
    w.step(10 * t.GRID)
    e = w.v.epoch
    w.poke()
    check("unverified" in w.v.rec(e)["flags"] or not w.v.hook.gapSinceVerify, "gap seen")
    e = w.v.epoch
    w.step(1)
    w.poke()
    check("unverified" in w.v.rec(e)["flags"], "records: unverified flag while a gap is open")
    w = World()
    w.place(T0 - 1000, T0 + 1000)
    w.keepalive_run(t.COOLDOWN)
    w.v.baseOwed = 12345
    w.ref.fail = lambda end: True
    w.step(3 * t.GRID)
    fv0 = sum(r["forfeitValue"] for r in w.v.records.values())
    w.v.retire(w.block, w.ts())
    w.ref.fail = set()
    check(sum(r["forfeitValue"] for r in w.v.records.values()) >= fv0 + 12345, "records: retire forfeitValue includes baseOwed")
    # the roll (§3.9, §4.6 step 6; s19): a gap of V + 3 epochs closes `old` (stale), writes one stale gap record with an
    # empty window and opens the new record with witnessMissing; at most two EpochRecorded; tauV is the difference
    w = World()
    w.place(T0 - 1000, T0 + 1000)
    w.keepalive_run(t.COOLDOWN)
    old = w.v.epoch
    tv_start = w.v.rec(old)["tauV"]
    n_ev = len(w.v.events)
    w.step((R.V + 3) * t.EPOCH)
    w.poke()
    ne = w.v.epoch
    ro, rg, rn = w.v.rec(old), w.v.rec(old + 1), w.v.rec(ne)
    recs = [x for x in w.v.events[n_ev:] if x[0] == "EpochRecorded"]
    check(ne == old + R.V + 3 + (1 if (w.block // t.EPOCH) > ne else 0) or ne == w.block // t.EPOCH, "roll: epoch advanced")
    check(len(recs) == 2 and recs[0][1] == old and recs[1][1] == old + 1, "roll: at most two records, old and the gap", recs)
    check(ro["end"] == w.block and "stale" in ro["flags"] and ro["tauV"] == w.v.hook.tauV - tv_start,
          "roll: old closed, stale, tauV as a difference", (ro["end"], ro["flags"], ro["tauV"]))
    check(rg["start"] == rg["end"] == w.block and rg["flags"] == {"stale", "witnessMissing"}, "roll: the gap record", rg)
    check(rn["start"] == w.block and rn["end"] == 0 and "witnessMissing" in rn["flags"], "roll: new record opened", rn)
    w.v.poke(w.block, w.ts(), roll=(1, 10**18, True))
    check("witnessMissing" not in rn["flags"] and "witnessFallback" in rn["flags"], "witness: a live roll clears missing")
    w.v.poke(w.block, w.ts(), roll=(0, 0, False))
    check("witnessMissing" not in rn["flags"], "witness: an ignored roll writes nothing")
    w.v.poke(w.block, w.ts(), roll=(2, 0, False))
    check("witnessMissing" in rn["flags"], "witness: a failed roll sets missing")
    w.v.poke(w.block, w.ts(), roll=(1, 0, False))
    check("witnessMissing" in rn["flags"] and "witnessFallback" not in rn["flags"], "witness: lw = 0 is missing")
    # deposit and withdraw return the exact amounts paid and received (idle slice + liquidity leg)
    w = World()
    w.place(T0 - 1000, T0 + 1000)
    w.step(1)
    w.poke()
    w.settle_due()
    S, i0, i1, L = w.v.S, w.v.h.idle0, w.v.h.idle1, w.v.h.L
    clone = _copy.deepcopy(w.v.pool)
    sh = 10 ** 24 + 3
    a0, a1, _, _ = clone.modify("vault", w.v.h.lo, w.v.h.hi, R.ceil_div(L * sh, S))
    paid = w.deposit(sh)
    check(paid == (R.ceil_div(i0 * sh, S) + a0, R.ceil_div(i1 * sh, S) + a1), "deposit returns idle slice + liquidity leg",
          (paid, a0, a1))
    S, i0, i1, L = w.v.S, w.v.h.idle0, w.v.h.idle1, w.v.h.L
    clone = _copy.deepcopy(w.v.pool)
    sh = 10 ** 23 + 9
    a0, a1, _, _ = clone.modify("vault", w.v.h.lo, w.v.h.hi, -(L * sh // S))
    try:
        got = w.withdraw(sh)
    except (R.Revert, R.MathRevert) as ex:
        got = repr(ex)
    check(got == (i0 * sh // S - a0, i1 * sh // S - a1), "withdraw returns idle slice + liquidity leg", (got, a0, a1))


def rules_r4b():
    """Clause checks from the generated statement-deletion class."""
    t = R.TEST
    # seed initialises epoch, NAV0 at the cached reference, segStart, and records its own snapshot
    w = World()
    v = w.v
    check(v.epoch == w.block // t.EPOCH and v.segStart == w.block and v.NAV0 == v.nav(sqrt_at(v.hook.refTick)) > 0,
          "seed initialises epoch, segStart and NAV0 at the cache")
    check(v.hook.ring and v.hook.ring[0][0] == w.block and v.hook.lastSnap == w.block, "seed records its snapshot")
    # the checkpoint sets segStart := now
    w.keepalive_run(30)
    w.step(1)
    w.poke()
    check(w.v.segStart == w.block, "the checkpoint closes the segment at now")
    # an action whose prologue did not read the live reference reads it exactly once
    r0 = getattr(w.ref, "live_count", 0)
    w.v.rebalance_swap(False, 10 ** 15, w.block, w.ts())
    check(getattr(w.ref, "live_count", 0) == r0 + 1, "an action reads the live reference once", getattr(w.ref, "live_count", 0) - r0)
    # records: bank and bond burned by a charge
    w = World(seed_amounts=(150_000 * USDC, 500 * ETH), third_L=10 ** 19)
    w.v.pool.push_to(sqrt_at(T0 - 55))
    w.step(1)
    bond0 = w.v.bank.bondShares
    c, p, burn = w.v.rebalance_swap(False, w.v.nav(sqrt_at(T0)) // 100, w.block, w.ts())
    r = w.v.rec(w.v.epoch)
    check(burn > 0 and r["bondBurned"] == bond0 - w.v.bank.bondShares - w.v.bank.reserveBond and r["bankBurned"] + r["bondBurned"] == burn,
          "records: bank and bond burned", (r["bankBurned"], r["bondBurned"], burn))
    # records: bank burned (with unvested pay present)
    w = World(seed_amounts=(300_000 * USDC, 20 * ETH), third_L=10 ** 19)
    w.place(T0, T0 + 2000)
    w.deposit(10 ** 26)
    w.v.pool.push_to(sqrt_at(T0 + 30))  # strictly inside the range, so τ_v accrues
    w.keepalive_run(t.EPOCH)
    w.poke()
    unv = w.v.bank.unvested()
    w.v.pool.push_to(sqrt_at(T0 + 55))
    w.keepalive_run(1)
    rb = w.v.rec(w.v.epoch)
    b0, d0, bond0 = rb["bankBurned"], rb["bondBurned"], w.v.bank.bondShares
    c, p, burn = w.v.rebalance_swap(False, w.v.h.idle1 // 50, w.block, w.ts())
    rb = w.v.rec(w.v.epoch)
    check(unv > 0 and burn > unv and w.v.bank.unvested() == 0 and rb["bankBurned"] - b0 > 0 and
          rb["bondBurned"] - d0 == bond0 - w.v.bank.bondShares - w.v.bank.reserveBond and rb["bankBurned"] - b0 + rb["bondBurned"] - d0 == burn,
          "records: bank then bond burned", (burn, unv))
    # registration zeroes the streak; epochStartRef is the epoch's first cache write
    w = World()
    h = w.v.hook
    w.keepalive_run(20)
    check(h.epochStartRefEpoch == w.block // t.EPOCH or h.epochStartRefEpoch == (w.block - 20) // t.EPOCH, "epoch start ref set")
    h.resetStreak = 1
    w.place(T0 - 1000, T0 + 1000)
    check(h.resetStreak == 0, "registration zeroes the reset streak")
    e = w.block // t.EPOCH
    w.keepalive_run(t.EPOCH - w.block % t.EPOCH + 3)
    first = [ev for ev in h.events if ev[0] == "RefRefreshed"][-1]
    check(h.epochStartRefEpoch == e + 1 and h.epochStartRef == first[2], "epochStartRef = the epoch's first cache write")
    # τ_b counts spans with tickNow inside the witness range
    h = R.Hook(t, R.RefSource(lambda ts: T0))
    h.armed, h.refTick, h.refGrid, h.refCachedAt, h.lastSnap, h.ring = True, T0, 10 ** 9, 1000, 1000, [(1000, T0)]
    h.witLo, h.witHi = T0 - 100, T0 + 100
    h.snapshot(1020, 10 ** 12, sqrt_at(T0), T0)
    h.snapshot(1030, 10 ** 12, sqrt_at(T0 + 100), T0 + 100)
    check(h.tauB == 20, "τ_b counts the witness range [lo, hi)", h.tauB)


def rules_r5():
    """Checks added after fix review r5 and the generated operand class."""
    import copy as _copy
    t = R.TEST
    # multi-epoch accrual without claims: every share stays accounted for (gate-open overwrites add to vested)
    w = World()
    w.place(T0 - 1000, T0 + 1000)
    w.deposit(10 ** 26)
    for _ in range(9):
        w.keepalive_run(t.EPOCH)
        w.poke()
    conserved(w, "nine epochs without a claim")
    check(w.v.bank.vestedUnclaimed > 0, "pay vested over nine epochs")
    # the solvency gate counts unvested pay: bond < burn <= unvested + bond passes
    h0 = R.Holdings(10 ** 12, 10 ** 21)
    h1 = R.Holdings(10 ** 12, 10 ** 21 - 10 ** 18)
    c, p, n, burn, ok = R.measure(h0, h1, sqrt_at(T0), sqrt_at(T0), 10 ** 24, 0, 0)
    ok2 = R.measure(h0, h1, sqrt_at(T0), sqrt_at(T0), 10 ** 24, burn - 10, 10)[4]
    ok3 = R.measure(h0, h1, sqrt_at(T0), sqrt_at(T0), 10 ** 24, burn - 11, 10)[4]
    check(burn > 20 and ok2 and not ok3, "gate: burn <= unvested + bond, both legs count")
    # tier constants: m_perBlock = 1%/yr per block (WAD), pinned exactly
    check((R.TEST.m_perBlock, R.DEMO.m_perBlock, R.MAINNET.m_perBlock) ==
          (10 ** 16 * 12 // 31536000, 10 ** 16 // 31536000, 10 ** 16 * 12 // 31536000), "m_perBlock per tier")
    # Obs index fields
    w = World()
    o = w.v.hook.obs(w.block + 7, 0)
    check(o["gridIndex"] == (w.block + 7) // t.GRID and o["epoch"] == (w.block + 7) // t.EPOCH, "Obs grid and epoch")
    # frozen records over a span with two boundaries, τ_v and τ_b credited; and the last-8 cap over a long span
    h = R.Hook(t, R.RefSource(lambda ts: T0))
    h.armed, h.vaultLo, h.vaultHi, h.emptyRange = True, T0 - 1000, T0 + 1000, False
    h.witLo, h.witHi = T0 - 100, T0 + 100
    h.refTick, h.refGrid, h.refCachedAt, h.lastSnap, h.ring, h.med9 = T0, 10 ** 9, 1005, 1005, [(1005, T0)], T0
    h.tauV, h.tauB = 3, 4
    h.snapshot(1043, 10 ** 12, sqrt_at(T0), T0)
    check(h.frozen.get(51) == (T0, 3 + 15, 4 + 15) and h.frozen.get(52) == (T0, 3 + 35, 4 + 35) and h.tauB == 4 + 38,
          "frozen τ_v(B), τ_b(B) and τ_b over the span", (h.frozen, h.tauB))
    h2 = R.Hook(t, R.RefSource(lambda ts: T0))
    h2.armed, h2.vaultLo, h2.vaultHi, h2.emptyRange = True, T0 - 1000, T0 + 1000, False
    h2.refTick, h2.refGrid, h2.refCachedAt, h2.lastSnap, h2.ring, h2.med9 = T0, 10 ** 9, 1000, 1000, [(1000, T0)], T0
    h2.snapshot(1000 + 12 * t.GRID + 5, 10 ** 12, sqrt_at(T0), T0)
    keys = sorted(h2.frozen)
    check(len(keys) == 8 and keys[-1] == (1000 + 12 * t.GRID + 5) // t.GRID and keys[0] == keys[-1] - 7 and
          all(h2.frozen[k][1] == 2 * t.GRID for k in keys if k * t.GRID >= 1000 + 2 * t.GRID),
          "freeze at most the last 8 boundaries; credit capped at freshness", keys)
    # _refresh reads the anchored sample (phase-offset timestamps): seed's cache is the anchor's tick
    f = lambda end: T0 + (end * 7919) % 101  # non-periodic, so reads ending at different times differ
    w = World(ref_fn=f, phase=150)
    k = w.ts() // t.GRID_SEC
    check(w.v.hook.refTick == f(k * t.GRID_SEC) and len({f(k * t.GRID_SEC), f(0), f(t.GRID_SEC), f(w.ts() - t.GRID_SEC),
                                                          f(w.ts())}) == 5,
          "the seed refresh reads the TWAP ending at the anchor", w.v.hook.refTick)
    # a buy (token1 in) through thin depth stops at spot + (FEE_TICKS + BAND_SWAP)
    w = World(third_L=10 ** 14)
    amt = min(w.v.h.idle1, w.v.nav(sqrt_at(T0)) * 19 // 1000)
    w.v.rebalance_swap(False, amt, w.block, w.ts())
    check(w.v.pool.s == sqrt_at(T0 + 10), "a buy stops exactly at spot + 10 ticks", w.v.pool.tick)
    # an inactive pre-swap does nothing
    w = World(seed_amounts=(300_000 * USDC, 20 * ETH))
    w.place(T0, T0 + 2000, dict(active=False, zeroForOne=True, amountIn=10 ** 9))
    check(w.v.swapCount == 0 and w.v.epochSwapFrac == 0, "an inactive pre-swap does not swap")
    # a failed live read never opens the gate, even when tick 0 is inside the range
    w = World()
    v = w.v
    hk = v.hook
    hk.vaultLo, hk.vaultHi, hk.emptyRange = -1000, 1000, False
    hk.refTick, hk.refCachedAt, hk.gapSinceVerify = 0, w.block, False
    v.bank.cohorts[0] = [0, 10 ** 18]
    v.bank.placedByCurator = True
    w.ref.fail = lambda end: True
    o = hk.obs(w.block, 0)
    v._checkpoint(o, w.block, w.ts())
    check(v.bank.vestedUnclaimed == 0 and v.bank.maturedPending == 10 ** 18, "a failed live read closes the gate")
    w.ref.fail = set()
    # reads are counted at the reference double: an action reuses a successful prologue read
    w = World()
    w.place(T0 - 1000, T0 + 1000)
    w.keepalive_run(t.EPOCH)
    w.poke()
    w.keepalive_run(t.EPOCH * (1 + R.V))
    w.step(1)
    n0 = getattr(w.ref, "live_count", 0)
    w.v.rebalance_swap(False, 10 ** 15, w.block, w.ts())
    check(getattr(w.ref, "live_count", 0) - n0 == 1, "the action reuses the prologue's live read (one read)")
    w.keepalive_run(t.COOLDOWN)
    w.step(1)
    n0 = getattr(w.ref, "live_count", 0)
    w.v.rebalance_swap(False, 10 ** 15, w.block, w.ts())
    check(getattr(w.ref, "live_count", 0) - n0 == 1, "with no prologue read the action reads once")
    # exact pre-breach forfeitValue and exact record base
    st = {"ref": T0}
    w = World(ref_fn=lambda ts: st["ref"])
    w.place(T0 - 1000, T0 + 1000)
    w.deposit(10 ** 26)
    w.keepalive_run(t.EPOCH)
    w.poke()
    st["ref"] = T0 + 2500
    w.keepalive_run(t.CURE_BLOCKS + 3 * t.GRID)
    w.step(1)
    v = w.v
    o = v.hook.snapshot(w.block, w.ts(), v.pool.s, v.pool.tick)
    e = v.epoch  # the record of the stored epoch (a span is recorded under the epoch it opened in, §3.9)
    fv0, b0, lt0 = v.rec(e)["forfeitValue"], v.rec(e)["base"], v.rec(e)["lTau"]
    exp_cut = R.mul_div(R.mul_div(t.m_perBlock, v.NAV0, R.WAD), v.delta0 * (o["tauVAtLastPending"] - v.tauV0), R.WAD)
    exp_fv = exp_cut + v.baseOwed
    exp_lt = v.h.L * (o["tauV"] - v.tauV0)
    exp_base = R.mul_div(R.mul_div(t.m_perBlock, v.NAV0, R.WAD), v.delta0 * (o["tauV"] - o["tauVAtLastPending"]), R.WAD)
    v._collect_first()
    S0m = v.S
    navm = v.nav(sqrt_at(o["refTick"]))
    v._checkpoint(o, w.block, w.ts())
    check(v.S - S0m == exp_base * S0m // (navm - exp_base), "after a breach the mint covers only the base after b_last",
          (v.S - S0m, exp_base))
    check(o["pendingBreaches"] == 1 and v.rec(e)["forfeitValue"] - fv0 == exp_fv and exp_fv > 0,
          "record forfeitValue = the unminted base up to b_last", (v.rec(e)["forfeitValue"] - fv0, exp_fv))
    check(v.rec(e)["base"] - b0 == exp_base + exp_cut, "record base = the gross base, the cut span included (S19D-01)",
          (v.rec(e)["base"] - b0, exp_base, exp_cut))
    check(v.rec(e)["lTau"] - lt0 == exp_lt and exp_lt > 0, "record lTau = L·Δτ_v across the breach cut (S19D-01)",
          (v.rec(e)["lTau"] - lt0, exp_lt))
    check(v.baseOwed == 0, "a priced breach checkpoint leaves no baseOwed", v.baseOwed)
    # empty holdings: δ = 0 (no division by zero)
    try:
        d = R.delta_of(R.Holdings(), sqrt_at(T0))
    except Exception as ex:
        d = repr(ex)
    check(d == 0, "δ of empty holdings is 0", d)
    # a same-block refreshRef retries an anchor whose read failed in the block's snapshot
    w = World()
    w.keepalive_run(100)
    w.step(t.GRID - w.block % t.GRID + 1)
    w.ref.fail = lambda end: True
    w.poke()
    k_fail = w.v.hook.refGrid
    w.ref.fail = set()
    w.touch_same_block()
    check(w.v.hook.refCachedAt == w.block and w.v.hook.refGrid == w.ts() // t.GRID_SEC and w.v.hook.refGrid > k_fail,
          "a same-block refreshRef retries a failed anchor")
    # NAV' == the mint amount defers (no division by zero; every entry point stays live)
    w = World()
    w.place(T0 - 1000, T0 + 1000)
    w.deposit(10 ** 26)
    w.keepalive_run(100)
    w.step(1)
    v = w.v
    o = v.hook.snapshot(w.block, w.ts(), v.pool.s, v.pool.tick)
    base = R.mul_div(R.mul_div(t.m_perBlock, v.NAV0, R.WAD), v.delta0 * (o["tauV"] - v.tauV0), R.WAD)
    navp = v.nav(sqrt_at(v.hook.refTick))
    v.baseOwed = navp - base
    try:
        w.withdraw(10 ** 20)
        check(v.baseOwed == navp or w.v.baseOwed == navp, "NAV' == amount defers the whole amount", w.v.baseOwed)
    except (R.Revert, R.MathRevert) as ex:
        check(False, "NAV' == amount defers the whole amount", repr(ex))


def rules_med9():
    t = R.TEST
    # med9 is the lower median of the last 9 snapshots; the ring keeps N_SNAP; an unarmed snapshot records its entry
    h = R.Hook(t, R.RefSource(lambda ts: T0))
    try:
        for i, tk in enumerate([5, 1, 9, 3, 7, 2, 8, 4, 6, 100, 0] + list(range(40))):
            h.snapshot(1000 + i, 12 * (1000 + i), sqrt_at(T0 + tk), T0 + tk)
            last = sorted(x for _, x in h.ring[-9:])
            check(h.med9 == last[(len(last) - 1) // 2] and len(h.ring) == min(i + 1, R.N_SNAP), "med9 and ring size", i)
        check(h.ring[-1] == (1000 + i, T0 + 39) and h.lastSnap == 1000 + i, "unarmed snapshots record entries")
    except Exception as ex:
        check(False, "med9 and ring size", repr(ex))
    # an armed snapshot records its (block, tick) entry
    w = World()
    w.keepalive_run(7)
    check(w.v.hook.ring[-1] == (w.block, w.v.pool.tick) if w.v.hook.ring else False, "an armed snapshot records its entry")
    # frozen τ_v(B) is not credited for a span the clock does not credit to τ_v
    h = R.Hook(t, R.RefSource(lambda ts: T0 + 1500))
    h.armed, h.vaultLo, h.vaultHi, h.emptyRange = True, T0 - 1000, T0 + 1000, False
    h.refTick, h.refGrid, h.refCachedAt, h.lastSnap, h.ring, h.med9 = T0 + 1500, 10 ** 9, 1005, 1005, [(1005, T0)], T0
    h.tauV = 9
    h.snapshot(1043, 10 ** 12, sqrt_at(T0), T0)
    check(h.frozen.get(51) == (T0, 9, 0) and h.frozen.get(52) == (T0, 9, 0), "frozen τ_v(B) uncredited out of range", h.frozen)


def rules_r6():
    """Checks added after fix review r6: frozen boundaries at low lastSnap, the first-action cooldown convention,
    pinned constants, the release delay boundary."""
    t = R.TEST
    for last, now, want in ((25, 50, {2: 15}), (5, 30, {1: 15}), (0, 40, {1: 20, 2: 40}), (41, 99, {3: 19, 4: 39})):
        h = R.Hook(t, R.RefSource(lambda ts: T0))
        h.armed, h.vaultLo, h.vaultHi, h.emptyRange = True, T0 - 1000, T0 + 1000, False
        h.refTick, h.refGrid, h.refCachedAt, h.lastSnap, h.ring, h.med9 = T0, 10 ** 9, last, last, [(last, T0)], T0
        h.snapshot(now, 10 ** 12, sqrt_at(T0), T0)
        check({k: v[1] for k, v in h.frozen.items()} == want, "frozen boundaries exact at low lastSnap", (last, now, h.frozen))
    # the first curator action is never blocked by COOLDOWN (no previous action), even before block COOLDOWN
    w = World(start_block=3, phase=10 ** 6)
    try:
        w.place(T0 - 1000, T0 + 1000)
        check(w.v.lastAction == 3, "the first action is not subject to COOLDOWN")
    except R.Revert as ex:
        check(False, "the first action is not subject to COOLDOWN", ex.err)
    # constants (spec §3.2, §6), pinned as literals
    check((R.S_SEED, R.DEAD, R.N_SNAP, R.K_MED, R.V, R.TEST.MAX_SWAPS, R.TEST.DELTA_MIN, R.TEST.BOND_RATIO) ==
          (10 ** 24, 10 ** 15, 32, 9, 4, 20, 5 * 10 ** 17, 174 * 10 ** 13), "pinned constants")
    check((R.TEST.RESERVE_RATIO, R.TEST.H_MARKOUT, R.DEMO.H_MARKOUT, R.MAINNET.H_MARKOUT, R.TEST.NAV_MAX, R.DEMO.NAV_MAX,
           R.MAINNET.NAV_MAX) == (6 * 10 ** 14, 120, 20, 120, R.U128, 1850 * 10 ** 18, 1850 * 10 ** 18),
          "repair C constants (RESERVE_RATIO, H_MARKOUT, NAV_MAX per tier)")
    w = World()
    check(w.v.S == 10 ** 24 and w.v.bank.bondShares == 10 ** 24 - 10 ** 15 and w.v.bank.releaseDelay == 480 and
          w.v.bank.dripBlocks == 120, "seed: S_SEED, DEAD, releaseDelay = V·EPOCH, dripBlocks = EPOCH at TEST")
    # release one block before the delay reverts; at the delay it pays
    w.keepalive_run(t.COOLDOWN)
    w.v.retire(w.block, w.ts())
    ex_b = w.v.bank.exitBlock
    w.keepalive_run(479)
    try:
        w.release()
        check(False, "release at exit + 479 must revert")
    except R.Revert as ex:
        check(ex.err == "BondLocked", "release at exit + releaseDelay − 1 reverts", ex.err)
    w.step(1)
    check(w.block == ex_b + 480 and w.release() > 0, "release at exit + releaseDelay pays")
    # the pool double refuses an over-removal, as v4 does
    import probe_rt
    pool = R.Pool(sqrt_at(T0), 500)
    pool.modify("x", T0 - 100, T0 + 100, 10 ** 20)
    pool.swap(False, 10 ** 18, sqrt_at(T0 + 50))
    pool.swap(True, 10 ** 18, sqrt_at(T0 - 50))
    before = probe_rt.canon(pool)
    try:
        pool.modify("x", T0 - 100, T0 + 100, -(10 ** 20 + 1))
        check(False, "over-removal reverts")
    except R.MathRevert:
        check(probe_rt.canon(pool) == before, "over-removal reverts with the whole pool state unchanged")


def rules_r7():
    """Checks added after fix review r7: repeated stale spans, cooldown boundary, tier tables, one skipped epoch."""
    t = R.TEST
    # touches every block through an unreadable-reference silence credit only up to refCachedAt + 2·GRID
    for out_of_range in (False, True):
        st = {"ref": T0}
        w = World(ref_fn=lambda ts: st["ref"])
        w.place(T0 - 1000, T0 + 1000)
        w.deposit(10 ** 26)
        w.keepalive_run(360)
        w.poke()
        h = w.v.hook
        if out_of_range:
            st["ref"] = T0 + 1500
            w.step(t.GRID - w.block % t.GRID)
            w.touch()  # cache the out-of-range anchor
        tv0, ob0, seq0, last0, cached = h.tauV, h.outBlocks, h.breachSeq, h.lastSnap, h.refCachedAt
        w.ref.fail = lambda end: True
        frozen_tv = []
        for _ in range(150):
            w.step(1)
            w.touch()
        stale_at = cached + 2 * t.GRID
        exp = stale_at - last0
        if out_of_range:
            check(h.outBlocks - ob0 == exp and h.breachSeq == seq0 and h.tauV == tv0,
                  "repeated stale spans credit outBlocks only up to the freshness point", (h.outBlocks - ob0, exp))
        else:
            check(h.tauV - tv0 == exp and h.outBlocks == ob0, "repeated stale spans credit τ_v only up to freshness",
                  (h.tauV - tv0, exp))
            tvB = [v[1] for k, v in sorted(h.frozen.items()) if k * t.GRID > stale_at]
            check(tvB and all(x == tv0 + exp for x in tvB), "τ_v(B) flat after the staleness point", tvB[:3])
        w.ref.fail = set()
    # COOLDOWN boundary: a second action at lastAction + COOLDOWN − 1 reverts, at + COOLDOWN passes
    w = World()
    w.place(T0 - 1000, T0 + 1000)
    w.step(t.COOLDOWN - 1)
    try:
        w.place(T0 - 1010, T0 + 990)
        check(False, "an action at COOLDOWN − 1 must revert")
    except R.Revert as ex:
        check(ex.err == "Cooldown", "COOLDOWN − 1 reverts", ex.err)
    w.step(1)
    w.place(T0 - 1010, T0 + 990)
    # per-chain tables (spec §6), pinned as literals
    for tr, row in ((R.TEST, (12, 20, 240, 120, 200, 14, 600, 20, 120)), (R.DEMO, (1, 300, 300, 3600, 1800, 30, 300, 300, 3600)),
                    (R.MAINNET, (12, 300, 3600, 50400, 7200, 30, 600, 1800, 50400))):
        got = (tr.BLOCKTIME, tr.GRID, tr.GRID_SEC, tr.EPOCH, tr.CURE_BLOCKS, tr.COOLDOWN, tr.REF_WINDOW, tr.EDGE_DELAY,
               tr.FORFEIT_DRIP_BLOCKS)
        check(got == row, f"{tr.NAME} per-chain parameters", got)
        check((tr.FEE, tr.SPACING, tr.FEE_TICKS, tr.W_TICKS, tr.BAND_SWAP, tr.REF_BAND, tr.DELTA_MIN, tr.MAX_SWAP_ACTION,
               tr.MAX_SWAP_EPOCH, tr.MAX_SWAPS, tr.RESET_RUN, tr.BOND_RATIO, tr.L_W_TARGET) ==
              (500, 10, 5, 2000, 5, 60, 5 * 10 ** 17, 2 * 10 ** 16, 5 * 10 ** 16, 20, 2, 174 * 10 ** 13, 1000),
              f"{tr.NAME} shared parameters")
    # one skipped epoch flags exactly the skipped record stale
    w = World()
    w.place(T0 - 1000, T0 + 1000)
    e0 = w.v.epoch
    w.step((e0 + 2) * t.EPOCH + 3 - w.block)
    w.poke()
    check("stale" in w.v.rec(e0 + 1)["flags"] and w.v.epoch == e0 + 2, "one skipped epoch is flagged stale")


def rules_r7b():
    """Clause checks from the generated literal class."""
    t = R.TEST
    # module constants and tables, pinned
    check((R.U32, R.U40, R.U64, R.U128) == (2 ** 32 - 1, 2 ** 40 - 1, 2 ** 64 - 1, 2 ** 128 - 1), "integer bounds")
    check(R.PLACEMENT_ERR == {1: "Fence", 2: "Width", 3: "Alignment", 4: "NotContained", 5: "NotContained"},
          "placement error mapping")
    check(all(not R.ladder_allowed(m, R.E["Seed"]) for m in range(3)), "Ladder.allowed(·, Seed) is false")
    check((R.TEST.BOND_MIN, R.DEMO.BOND_MIN, R.MAINNET.BOND_MIN) == (0, 174 * 10 ** 16, 174 * 10 ** 16),
          "BOND_MIN per tier (owner decisions 2026-11-16: 1.74 WETH (s09); TEST 0)")
    # a placement that does not contain the reference reverts NotContained (code 5) at the vault
    w = World()
    w.v.pool.push_to(sqrt_at(T0 + 40))
    w.step(1)
    try:
        w.place(T0 + 10, T0 + 2010)
        check(False, "a range without the reference must revert")
    except R.Revert as ex:
        check(ex.err == "NotContained", "reference not contained reverts NotContained", ex.err)
    # a one-wei loss is charged one wei
    h0, h1 = R.Holdings(10 ** 12, 10 ** 21), R.Holdings(10 ** 12, 10 ** 21 - 1)
    check(R.measure(h0, h1, sqrt_at(T0), sqrt_at(T0), 10 ** 24, 10 ** 24, 0)[0] == 1, "a one-wei loss is charged")
    # seed state: δ0 = 0; fresh records start at zero; GapCleared kinds (0 registration, 1 reset)
    w = World()
    check(w.v.delta0 == 0, "seed δ0 = 0")
    r = w.v.rec(10 ** 6)
    check((r["base"], r["charge"], r["minted"], r["bankBurned"], r["bondBurned"], r["forfeited"], r["forfeitValue"],
           r["tauV"], r["flags"]) == (0, 0, 0, 0, 0, 0, 0, 0, set()), "a fresh record starts at zero")
    w.place(T0 - 1000, T0 + 1000)
    check(("GapCleared", 0) in w.v.hook.events, "registration emits GapCleared(0)")
    h = R.Hook(t, R.RefSource(lambda ts: T0))
    h.armed, h.vaultLo, h.vaultHi, h.emptyRange = True, T0 - 1000, T0 + 1000, False
    h.refTick, h.refGrid, h.refCachedAt, h.lastSnap, h.ring, h.resetStreak = T0, 100, 2000, 2000, [(2000, T0)], 1
    h.snapshot(2020, 2020 * t.BLOCKTIME, sqrt_at(T0), T0)
    check(("GapCleared", 1) in h.events, "a reset emits GapCleared(1)")
    # the pool position always equals the vault's L (collect-first is a zero-liquidity modify)
    w = World()
    w.place(T0 - 1000, T0 + 1000)
    for _ in range(4):
        w.keepalive_run(7)
        w.poke()
    check(w.v.pool.pos[("vault", w.v.h.lo, w.v.h.hi)][0] == w.v.h.L, "pool position L == vault L after prologues")
    # an ineligible (unplaced) vault's checkpoints add nothing to the record base
    w = World()
    w.keepalive_run(40)
    e = w.block // t.EPOCH
    b0 = w.v.rec(e)["base"]
    w.poke()
    check(w.v.rec(e)["base"] == b0, "no record base while ineligible")


def rules_r7c():
    """Clause checks from the literal class (second pass)."""
    t = R.TEST
    # the ring keeps the 32 newest snapshots
    h = R.Hook(t, R.RefSource(lambda ts: T0))
    for i in range(45):
        h.snapshot(1000 + i, 12 * (1000 + i), sqrt_at(T0), T0)
    check([b for b, _ in h.ring] == list(range(1000 + 45 - 32, 1000 + 45)), "the ring keeps the 32 newest snapshots")
    # the checkpoint is skipped before seed (S == 0) and returns no live read
    w = World()
    v = w.v
    S, h0 = v.S, v.h.d()
    v.S = 0
    v.hook.pendingBreaches, v.hook.lastPendingBlock, v.hook.tauVAtLastPending = 1, w.block, v.hook.tauV
    b0 = v.bank.d()
    r = v._checkpoint(v.hook.obs(w.block, 0), w.block, w.ts())
    check(r == (False, 0) and v.h.d() == h0 and v.bank.d() == b0, "S == 0: the checkpoint is skipped", r)
    v.S = S
    v.hook.pendingBreaches = 0
    # deposit and withdraw on an unplaced vault return exactly the idle slices
    w = World()
    S, i0, i1 = w.v.S, w.v.h.idle0, w.v.h.idle1
    sh = 10 ** 23 + 1
    paid = w.deposit(sh)
    check(paid == (R.ceil_div(i0 * sh, S), R.ceil_div(i1 * sh, S)), "unplaced deposit returns the idle slices", paid)
    S, i0, i1 = w.v.S, w.v.h.idle0, w.v.h.idle1
    got = w.withdraw(sh)
    check(got == (i0 * sh // S, i1 * sh // S), "unplaced withdraw returns the idle slices", got)


def rules_r7d():
    """One-unit boundaries from the literal class."""
    t = R.TEST
    # a single matured share (in maturedPending or in a cohort) is vestable and vests
    for where in ("pending", "cohort"):
        w = World()
        w.place(T0 - 1000, T0 + 1000)
        w.keepalive_run(30)
        b = w.v.bank
        if where == "pending":
            b.maturedPending = 1
        else:
            b.cohorts[0] = [0, 1]
        vu = b.vestedUnclaimed
        w.step(1)
        w.poke()
        check(b.vestedUnclaimed == vu + 1, f"a single matured share vests ({where})", b.vestedUnclaimed - vu)
    # a one-wei mint amount is minted (not dropped)
    w = World()
    w.place(T0 - 1000, T0 + 1000)
    w.keepalive_run(30)
    w.step(1)
    w.poke()
    w.v.baseOwed = 1
    v = w.v
    o = v.hook.snapshot(w.block, w.ts(), v.pool.s, v.pool.tick)  # same block: no new τ_v
    S0 = v.S
    v._checkpoint(o, w.block, w.ts())
    check(v.baseOwed == 0 and v.S > S0, "a one-wei amount is minted", (v.baseOwed, v.S - S0))
    # deposits and withdrawals that move exactly one unit of liquidity
    w = World()
    w.place(T0 - 1000, T0 + 1000)
    L0 = w.v.h.L
    w.deposit(1)
    check(w.v.h.L == L0 + 1, "a deposit moving one unit of L adds it", w.v.h.L - L0)
    sh = w.v.S // w.v.h.L + 1
    L1 = w.v.h.L
    w.withdraw(sh)
    check(w.v.h.L == L1 - L1 * sh // (w.v.S + sh) and L1 * sh // (w.v.S + sh) == 1, "a withdrawal moving one unit of L removes it",
          (L1 - w.v.h.L,))


def rules_r8():
    """Fix review r8 MINORs: seed epoch at a non-aligned block; hook counters saturate at their caps."""
    t = R.TEST
    w = World(start_block=12_001)
    check(w.v.epoch == 100, "seed epoch = floor(block / EPOCH)", w.v.epoch)
    w.place(T0 - 1000, T0 + 1000)
    w.step(12_120 - w.block)
    w.poke()
    check(w.v.records[100].get("end") == 12_120 and w.v.epoch == 101 and w.v.swapCount == 0, "roll at the epoch boundary")
    h = R.Hook(t, R.RefSource(lambda ts: T0 + 3000))
    h.armed, h.vaultLo, h.vaultHi, h.emptyRange = True, T0 - 1000, T0 + 1000, False
    h.refTick, h.refGrid, h.refCachedAt, h.lastSnap, h.ring = T0 + 3000, 10 ** 9, 1000, 1000, [(1000, T0)]
    h.outBlocks, h.breachSeq, h.pendingBreaches, h.tauB = R.U64 - 5, R.U32, R.U32, R.U64 - 1
    h.witLo, h.witHi = T0 - 100, T0 + 100
    try:
        h.snapshot(1040, 10 ** 12, sqrt_at(T0), T0)
        ok = (h.outBlocks, h.breachSeq, h.pendingBreaches, h.tauB) == (R.U64, R.U32, R.U32, R.U64)
    except (R.Revert, R.MathRevert) as ex:
        ok = repr(ex)
    check(ok is True, "hook counters saturate at their caps", ok)


def headroom_exact(rng):
    for _ in range(200):
        lo = rng.randrange(T0 - 1900, T0 - 100, 10)
        idle = R.Holdings(rng.randint(10 ** 6, 10 ** 13), rng.randint(10 ** 12, 10 ** 22))
        sp = sqrt_at(T0) + rng.randint(0, 10 ** 9)
        L0 = R.get_liquidity_for_amounts(sp, sqrt_at(lo), sqrt_at(lo + 2000), idle.idle0, idle.idle1)
        L, _, _ = R.placement_size(idle, lo, lo + 2000, sp, sqrt_at(T0))
        check(L == (L0 - (L0 // 10 ** 9 + 1) if L0 > L0 // 10 ** 9 + 1 else 0), "L = L0 − (L0/1e9 + 1)", (L, L0))


CRASHES = []


def _run(fn, *a):
    try:
        fn(*a)
    except (R.Revert, R.MathRevert) as ex:
        FAILS.append(f"{fn.__name__}: unexpected revert or assertion {ex!r}")
    except Exception as ex:  # a crash is not a check failure (mutation runner counts it as invalid)
        import traceback
        CRASHES.append(f"{fn.__name__} crashed: {ex!r}\n{traceback.format_exc()[-600:]}")


def rules_markout():
    """Repair C (the markout-settled charge): U-settle and the reserve rows of U-solv."""
    import copy as _copy
    t = R.TEST
    BT, H = t.BLOCKTIME, t.H_MARKOUT
    # tier assertion: COOLDOWN·blocktime ≥ H_MARKOUT + 2·blocktime
    try:
        R.Tier(**{**t.__dict__, "COOLDOWN": 11})
        check(False, "a COOLDOWN shorter than the markout window must fail the tier check")
    except R.TierInvalid:
        check(True, "tier check: COOLDOWN·blocktime ≥ H_MARKOUT + 2·blocktime")
    R.Tier(**{**t.__dict__, "COOLDOWN": 12})  # 144 ≥ 144 passes
    # measure_c: legs in the order spot, s0, ref; ties keep spot; reserve shares; the two-leg form agrees
    rng = random.Random(7)
    for _ in range(300):
        h = R.Holdings(rng.randrange(1, 10 ** 22), rng.randrange(1, 10 ** 22))
        h2 = R.Holdings(h.idle0 + rng.randrange(-10 ** 20, 10 ** 20), h.idle1 + rng.randrange(-10 ** 20, 10 ** 20))
        h2.idle0, h2.idle1 = max(1, h2.idle0), max(1, h2.idle1)
        sp, s0, sr = (sqrt_at(T0 + rng.randrange(-80, 80)) for _ in range(3))
        S, res = 10 ** 24, rng.randrange(0, 10 ** 19)
        c, p, n, b, br, ok = R.measure_c(h, h2, s0, sp, sr, res, S, 10 ** 30, 0)
        losses = [(R.value(h, x)[0] - R.value(h2, x)[0], x) for x in (sp, s0, sr)]
        best = losses[0]
        for l in losses[1:]:
            if l[0] > best[0]:
                best = l
        check(c == max(0, best[0]) and p == best[1], "measure_c: worst of spot, s0, ref; earlier leg on ties")
        na = min(R.value(h2, x)[0] for x in (sp, s0, sr))
        check(b == (0 if c == 0 else R.ceil_div(c * S, na)) and br == (0 if c + res == 0 else R.ceil_div((c + res) * S, na)) - b,
              "measure_c: burn and burnR at one price, the smallest NAV_after over the legs")
        check(R.measure(h, h2, sp, sr, S, 10 ** 30, 0)[:4] == R.measure_c(h, h2, sp, sp, sr, 0, S, 10 ** 30, 0)[:4],
              "the two-leg measure is measure_c with s0 = spot and no reserve")
    # settle_extra (s15, the fee-aware settlement): three legs (p_mk − FEE_TICKS, p_mk, p_mk + FEE_TICKS + 1), each
    # holding valued with the arbitrage fee its position earns from its start spot to the leg, the leg chosen by g/nb,
    # the burn re-priced at the markout (other holders whole at that leg), extra = the burn's value.
    h = R.Holdings(10 ** 21, 10 ** 21)
    h2 = R.Holdings(10 ** 21 + 10 ** 18, 10 ** 21 - 3 * 10 ** 15)
    S_act = 10 ** 24
    for tk in (T0 - 300, T0 - 50, T0, T0 + 50, T0 + 300):
        for burn0 in (0, 10 ** 17, 10 ** 22):
            for rsh in (0, 10 ** 18, 10 ** 22):
                pend = dict(s0=sqrt_at(T0), sEnd=sqrt_at(T0), charge0=0, reserve=0, burn0=burn0, sAct=S_act)
                ex, pm, bs = R.settle_extra(h, h2, pend, tk, 5, 500, rsh)
                legs = []
                for x in (sqrt_at(tk - 5), sqrt_at(tk), sqrt_at(tk + 6)):
                    nb = R.value(h, x)[0]
                    legs.append((nb - R.value(h2, x)[0], nb, x))
                pos = [l for l in legs if l[0] > 0]
                # the leg needing the most shares: the largest g/nb (by exact fractions), the earliest on ties
                want = max(pos, key=lambda l: (Fraction(l[0], l[1]), -legs.index(l))) if pos else legs[0]
                check(pm == want[2], "settle_extra: the leg with the largest loss fraction g/nb (idle holdings)", (tk, pm))
                loss = max(0, want[0])
                req = 0 if loss == 0 else -(-loss * S_act // want[1])
                exp_bs = min(rsh, max(0, req - burn0))
                check(bs == exp_bs, "settle_extra: burnS = min(reserveShares, ceilDiv(loss·S_act, nb) − burn0)", (bs, exp_bs))
                check(ex == (exp_bs * want[1] // S_act), "settle_extra: extra is the burn's value at nb/S_act", (ex, bs))
                if exp_bs < rsh and loss > 0:
                    B = burn0 + bs
                    check(B >= burn0 and (S_act - B) * want[1] <= S_act * (want[1] - loss),
                          "settle_extra: uncapped, the other holders are whole at the chosen leg")
    for tk in (R.MAX_TICK, R.MAX_TICK - 5, R.MIN_TICK, R.MIN_TICK + 4):   # clamped legs never leave the tick range
        pend = dict(s0=sqrt_at(T0), sEnd=sqrt_at(T0), charge0=0, reserve=0, burn0=0, sAct=S_act)
        ex, pm, bs = R.settle_extra(h, h2, pend, tk, 5, 500, 10 ** 20)
        check(sqrt_at(R.MIN_TICK) <= pm <= sqrt_at(R.MAX_TICK), "settle_extra: legs clamped at the tick bounds", tk)
    # F against the pool simulation: an arbitrage through a lone position from s to p (both directions; in range,
    # partial overlap, a one-tick overlap, out of range): F(…, up) ≥ the fees the position collects ≥ the credit
    for (lo, hi, s_t, p_t) in ((T0 - 1000, T0 + 1000, T0 + 60, T0 - 110), (T0 - 1000, T0 + 1000, T0 - 50, T0 + 170),
                               (T0 - 60, T0 + 1940, T0 + 60, T0 - 110), (T0 - 60, T0 + 1940, T0 - 59, T0 - 300),
                               (T0 + 200, T0 + 2200, T0 + 60, T0 - 110), (T0 - 10, T0 + 10, T0 + 300, T0 - 300),
                               (T0 - 2000, T0, T0 - 40, T0 + 500)):
        for L in (10 ** 18, 10 ** 22, 7 * 10 ** 24):
            pool = R.Pool(sqrt_at(s_t) + 7, 500)
            pool.modify("lp", lo, hi, L)
            pool.push_to(sqrt_at(p_t))
            f0, f1 = pool.collect("lp", lo, hi)
            pp = sqrt_at(p_t)
            got = R.q(f0, pp) + f1
            hh = R.Holdings(0, 0, L, lo, hi)
            up = R.arb_fee(hh, sqrt_at(s_t) + 7, pp, True, 500)
            dn = R.arb_fee(hh, sqrt_at(s_t) + 7, pp, False, 500)
            check(up >= got >= dn, "arb_fee: the closed form brackets the simulated pool's collected fee",
                  (lo, hi, s_t, p_t, L, up, got, dn))
            check(R.arb_fee(R.Holdings(10 ** 20, 10 ** 20), sqrt_at(s_t), pp, True, 500) == 0, "arb_fee: idle earns nothing")
    # FeeBank Charge/Settle: reserve composition, burn order, release targets
    b = R.Bank()
    b.cohorts[7 % R.RING] = [7, 100]
    b.maturedPending, b.bondShares = 50, 1000
    try:
        R.fee_bank_apply(_copy.deepcopy(b), "Charge", dict(burn=1000, burnR=151))
        check(False, "Charge: burn + burnR above the cover must revert Insolvent")
    except R.Revert as ex:
        check(ex.err == "Insolvent", "Charge gate includes the reserve", ex.err)
    b1 = _copy.deepcopy(b)
    r = R.fee_bank_apply(b1, "Charge", dict(burn=120, burnR=80))
    check(r == (120, 0, 120, 0) and b1.reserveShares == 80 and b1.reserveBond == 50 and b1.unvested() == 0 and
          b1.bondShares == 950, "Charge: burn youngest first, then the reserve in the same order", (r, b1.d()))
    b2 = _copy.deepcopy(b1)
    r = R.fee_bank_apply(b2, "Settle", dict(burnS=40, epoch=9, now=1000))
    check(r == (40, 0, 40, 0) and b2.bondShares == 990 and b2.reserveShares == 0, "Settle: the non-bond part burns first", r)
    b3 = _copy.deepcopy(b1)
    r = R.fee_bank_apply(b3, "Settle", dict(burnS=10, epoch=9, now=1000))
    check(r == (10, 0, 50, 20) and b3.cohorts[9 % R.RING] == [9, 20], "Settle: the rest of the non-bond part goes to the current cohort", r)
    b4 = _copy.deepcopy(b1)
    R.fee_bank_apply(b4, "Breach", dict(count=1, now=900))
    check(b4.reserveShares == 80 and b4.reserveForfeit, "Breach leaves the reserve in place and marks it")
    pool0 = b4.forfeitPool
    r = R.fee_bank_apply(b4, "Settle", dict(burnS=10, epoch=9, now=1000))
    check(r[3] == 20 and b4.forfeitPool >= 20 and b4.forfeitTotal == b4.forfeitPool and b4.forfeitStart == 1000 and
          b4.cohort_total() == 0, "Settle after a breach: the released non-bond part joins the forfeit drip", (r, pool0, b4.d()))
    b5 = _copy.deepcopy(b1)
    R.fee_bank_apply(b5, "Retire", dict(now=900))
    check(b5.reserveShares == 80 and b5.reserveForfeit, "Retire leaves the reserve in place and marks it")
    b5.releaseDelay = 0
    x = R.fee_bank_apply(b5, "Release", dict(shares=10 ** 9, now=10 ** 6))[1]
    check(x == 950 and b5.reserveBond == 50, "releaseBond cannot take the reserve's bond part", x)
    R.fee_bank_apply(b5, "Settle", dict(burnS=0, epoch=9, now=10 ** 6))
    check(b5.bondShares == 50, "the reserve's bond part returns to the bond at settlement")
    b6 = _copy.deepcopy(b1)
    b6.vestedUnclaimed = 5
    check(R.fee_bank_apply(b6, "Claim", {})[1] == 5 and b6.reserveShares == 80, "claim cannot reach the reserve")
    # the vault: pending record, deposits blocked, the window read, settlement, withdrawals never blocked
    reads = []

    def win(start, end):
        reads.append((start, end))
        return T0 + 40
    w = World()
    w.ref.window = win
    ch, p, burn, d = w.place(T0 - 1000, T0 + 1000)
    pd = w.v.pending
    check(pd is not None and pd["tAction"] == w.ts() and pd["charge0"] == ch and
          pd["reserve"] == R.mul_div(t.RESERVE_RATIO, R.value(pd["before"], sqrt_at(T0))[0], R.WAD) and
          w.v.bank.reserveShares > 0, "an action records the pending markout and reserves its shares")
    t_a = pd["tAction"]
    sact0, m_act = pd["sAct"], sum(r["minted"] for r in w.v.records.values())
    w.step(1)
    try:
        w.v.deposit(10 ** 20, w.block, w.ts())
        check(False, "a deposit while the markout is pending must revert")
    except R.Revert as ex:
        check(ex.err == "MarkoutPending", "deposits revert MarkoutPending", ex.err)
    out = w.v.withdraw(10 ** 18, w.block, w.ts())
    check(out[0] > 0 and w.v.pending is not None, "a withdrawal is never blocked by a pending markout")
    w.depositor -= 10 ** 18
    due = t_a + 2 * BT + H
    w.step((due - w.ts()) // BT - 1)
    w.v.settle(w.block, w.ts())
    check(w.v.pending is not None and not reads, "no settlement before tAction + 2·blocktime + H_MARKOUT")
    w.step(1)
    rs, S0, m0 = w.v.bank.reserveShares, w.v.S, sum(r["minted"] for r in w.v.records.values())
    h_now = w.v.h.copy()
    check(pd["sAct"] > 0 and pd["burn0"] >= 0 and pd["s0"] > 0 and pd["sEnd"] > 0, "the record keeps s0, sEnd, burn0, sAct")
    # the live record: base minted inside the window raised its sAct by exactly those shares (spec §3.4, S15D-07)
    check(w.v.pending["sAct"] == sact0 + (m0 - m_act) and m0 > m_act,
          "base minted inside the window raises the record's sAct by the shares minted", (w.v.pending["sAct"], sact0, m0 - m_act))
    pd = w.v.pending
    exp = R.settle_extra(pd["before"], pd["after"], pd, T0 + 40, t.FEE_TICKS, w.v.pool.fee, rs)
    w.v.settle(w.block, w.ts())
    check(reads == [(t_a + BT, t_a + BT + H)], "the window is (tAction + blocktime, tAction + blocktime + H_MARKOUT]", reads)
    minted = sum(r["minted"] for r in w.v.records.values()) - m0
    check(w.v.pending is None and w.v.bank.reserveShares == 0 and w.v.S == S0 - exp[2] + minted and
          ("Settled", pd["charge0"], exp[0], T0 + 40, False) in w.v.events, "settlement burns burnS and releases the rest",
          (S0 - w.v.S, exp))
    conserved(w, "after settlement")
    # an unreadable window burns the whole reserve, and the withdrawal in that call succeeds
    w = World()
    w.ref.fail_window = lambda a, b: True
    w.place(T0 - 1000, T0 + 1000)
    rs, S0, res = w.v.bank.reserveShares, w.v.S, w.v.pending["reserve"]
    m0 = sum(r["minted"] for r in w.v.records.values())
    w.step(13)
    out = w.v.withdraw(10 ** 18, w.block, w.ts())
    w.depositor -= 10 ** 18
    minted = sum(r["minted"] for r in w.v.records.values()) - m0
    check(out[0] > 0 and w.v.pending is None and w.v.S == S0 - rs - 10 ** 18 + minted and
          ("Settled", w.v.events[-1][1] if w.v.events else None) and any(e[0] == "Settled" and e[4] for e in w.v.events),
          "a failed window read burns the whole reserve; the withdrawal succeeds", (S0 - w.v.S, rs))
    conserved(w, "fallback settlement")
    # a breach while pending: the reserve follows at settlement into the forfeit drip
    st = {"ref": T0}
    w = World(ref_fn=lambda ts: st["ref"])
    w.place(T0 - 1000, T0 + 1000)
    st["ref"] = T0 + 2500
    w.keepalive_run(t.CURE_BLOCKS + 3 * t.GRID)
    check(w.v.pending is not None, "still pending (no vault call)")
    w.v.hook.pendingBreaches, rs = w.v.hook.pendingBreaches, w.v.bank.reserveShares
    w.poke()
    check(w.v.pending is None and w.v.bank.reserveShares == 0 and w.v.bank.mode == R.BREACHED,
          "a breach processed after settlement; nothing escapes")
    conserved(w, "breach after a pending action")
    # a new action settles the pending one first; NAV_MAX caps deposits at the re-based NAV0
    w = World()
    w.place(T0 - 1000, T0 + 1000)
    w.keepalive_run(t.COOLDOWN)
    w.v.rebalance_swap(False, 10 ** 15, w.block, w.ts())
    check(any(e[0] == "Settled" for e in w.v.events) and w.v.pending is not None, "a new action settles the pending one first")
    tier = R.Tier(**{**t.__dict__, "NAV_MAX": 0})
    w = World(tier=tier)
    try:
        w.deposit(10 ** 18)
        check(False, "a deposit above NAV_MAX must revert")
    except R.Revert as ex:
        check(ex.err == "NavCap", "NAV_MAX caps deposits", ex.err)
    # maxCharge and the vault's own move
    w = World()
    try:
        w.v.set_positions(T0 - 1000, T0 + 1000, R.Vault.NO_PRESWAP, w.block, w.ts(), max_charge=0)
        check(False, "charge0 + reserve above maxCharge must revert")
    except R.Revert as ex:
        check(ex.err == "MaxCharge", "maxCharge", ex.err)
    w = World(third_L=0)
    s_t = sqrt_at(T0 + 30) + 7
    ch, p, burn, d = w.v.set_positions(T0 - 1000, T0 + 1000, R.Vault.NO_PRESWAP, w.block, w.ts(), move_to=s_t)
    check(w.v.pending["before"].d() == R.Holdings(150_000 * USDC, 50 * ETH).d() and w.v.h.lo == T0 - 1000,
          "the move in an empty region is free; NAV_before uses the holdings before the move")
    w = World(third_L=10 ** 19)
    try:
        w.v.set_positions(T0 - 1000, T0 + 1000, R.Vault.NO_PRESWAP, w.block, w.ts(), move_to=sqrt_at(T0 + 4000))
        check(False, "a move the idle cannot pay for must revert")
    except R.Revert as ex:
        check(ex.err in ("SwapCap", "Fence"), "an unpayable or out-of-fence move reverts", ex.err)
    # the s0 leg: a pre-move spot away from the reference prices the action
    w = World(third_L=10 ** 17)
    w.v.pool.push_to(sqrt_at(T0 - 40))
    w.step(1)
    before = w.v.h.copy()
    ch, p, burn, d = w.v.set_positions(T0 - 1000, T0 + 1000, R.Vault.NO_PRESWAP, w.block, w.ts(), move_to=sqrt_at(T0))
    check(w.v.pending["before"].d() == before.d(), "the pending record keeps the holdings before the move")
    after, s0p, ssp = w.v.pending["after"], sqrt_at(T0 - 40), w.v.pool.s
    legs = [R.value(before, x)[0] - R.value(after, x)[0] for x in (s0p, sqrt_at(T0))]
    check(ch >= max(0, legs[0]) and ch == max([0] + legs + [R.value(before, R.get_sqrt_price_at_tick(T0))[0] -
                                                              R.value(after, R.get_sqrt_price_at_tick(T0))[0]]) or ch >= legs[0],
          "the pre-move spot s0 is a charge leg", (ch, legs))

    # --- mutation ratchets (repair C) ---
    # the move through third-party liquidity pays from idle: holdings after the move = before − in + out
    w = World(third_L=10 ** 17)
    clone = _copy.deepcopy(w.v.pool)
    ain, aout, zfo = clone.push_to(sqrt_at(T0 + 30))
    i0, i1 = w.v.h.idle0, w.v.h.idle1
    try:
        w.v.set_positions(T0 - 1000, T0 + 1000, R.Vault.NO_PRESWAP, w.block, w.ts(), move_to=sqrt_at(T0 + 30))
    except R.Revert:
        pass
    v2 = World(third_L=10 ** 17).v
    v2._remove_old()
    v2._move(sqrt_at(T0 + 30))
    check(not zfo and ain > 0 and aout > 0 and (v2.h.idle0, v2.h.idle1) == (i0 + aout, i1 - ain),
          "the vault's move pays the input from idle and receives the output", (ain, aout))
    v3 = World(third_L=10 ** 17).v
    c3 = _copy.deepcopy(v3.pool)
    a3, o3, z3 = c3.push_to(sqrt_at(T0 - 30))
    v3._move(sqrt_at(T0 - 30))
    check(z3 and (v3.h.idle0, v3.h.idle1) == (i0 - a3, i1 + o3), "a downward move pays token0 from idle")
    # SwapCap at the exact idle boundary, both directions
    for target, side in ((sqrt_at(T0 + 30), 1), (sqrt_at(T0 - 30), 0)):
        for short, want in ((0, None), (1, "SwapCap")):
            vv = World(third_L=10 ** 17).v
            cc = _copy.deepcopy(vv.pool)
            need = cc.push_to(target)[0]
            if side:
                vv.h.idle1 = need - short
            else:
                vv.h.idle0 = need - short
            try:
                vv._move(target)
                got = None
            except R.Revert as ex:
                got = ex.err
            check(got == want, "the move's idle check is exact on the paying side", (side, short, got))
    # maxCharge at its exact boundary and between charge0 and charge0 + reserve
    w = World(third_L=10 ** 17)
    w.v.pool.push_to(sqrt_at(T0 - 40))
    w.step(1)
    probe = _copy.deepcopy(w.v)
    probe.set_positions(T0 - 1000, T0 + 1000, R.Vault.NO_PRESWAP, w.block, w.ts(), move_to=sqrt_at(T0))
    c0, res = probe.pending["charge0"], probe.pending["reserve"]
    check(c0 > 0 and res > 0, "probe: a charged action with a reserve", (c0, res))
    for mc, want in ((c0 + res, None), (c0 + res - 1, "MaxCharge"), (c0 + res // 2, "MaxCharge")):
        vv = _copy.deepcopy(w.v)
        try:
            vv.set_positions(T0 - 1000, T0 + 1000, R.Vault.NO_PRESWAP, w.block, w.ts(), move_to=sqrt_at(T0), max_charge=mc)
            got = None
        except R.Revert as ex:
            got = ex.err
        check(got == want, "maxCharge bounds charge0 + reserve exactly", (mc - c0 - res, got))
    # settlement: the record's charge gains the extra; the released reserve enters the current epoch's cohort
    def win_hi(start, end):
        return T0 + 40
    w = World(tier=R.Tier(**{**t.__dict__, "m_perBlock": 0}))   # no base, so the cohort holds only the released shares
    w.ref.window = win_hi
    e_a = w.block // t.EPOCH
    w.v.bank.cohorts[e_a % R.RING] = [e_a, 10 ** 22]   # unvested pay, so the reserve is taken from the bank
    w.v.bank.bondShares -= 10 ** 22                     # (moved from the bond: supply unchanged)
    w.place(T0 - 1000, T0 + 1000)
    w.step(13)
    e_now = w.block // t.EPOCH
    w.v.bank.cohorts = [[e_now - 9, 0] for _ in range(R.RING)]   # isolate the released shares (stale epochs, empty)
    ch_before = w.v.rec(e_now)["charge"]
    rs, rb, S0 = w.v.bank.reserveShares, w.v.bank.reserveBond, w.v.S
    w.v.settle(w.block, w.ts())
    ev = [e for e in w.v.events if e[0] == "Settled"][-1]
    x = S0 - w.v.S
    rel = (rs - rb) - min(x, rs - rb)
    check(w.v.rec(e_now)["charge"] == ch_before + ev[2], "the settlement's extra is recorded in the epoch's charge")
    slot = w.v.bank.cohorts[e_now % R.RING]
    check(rs - rb > 0 and rel > 0 and slot == [e_now, rel] and sum(c[1] for c in w.v.bank.cohorts) == rel,
          "the released reserve enters the current epoch's cohort", (slot, e_now, rel, ev))
    # the fallback event carries (charge0, reserve, 0, True)
    w = World()
    w.ref.fail_window = lambda a, b: True
    w.place(T0 - 1000, T0 + 1000)
    pd = w.v.pending
    w.step(13)
    w.v.settle(w.block, w.ts())
    check(("Settled", pd["charge0"], pd["reserve"], 0, True) in w.v.events, "the fallback settlement event is exact")
    # NAV_MAX at its exact boundary (NAV0 after the re-base equal to the cap passes; one wei lower reverts)
    w = World()
    probe = _copy.deepcopy(w.v)
    probe.deposit(10 ** 22, w.block, w.ts())
    cap = probe.NAV0
    for nm, want in ((cap, None), (cap - 1, "NavCap")):
        vv = _copy.deepcopy(w.v)
        vv.t = R.Tier(**{**t.__dict__, "NAV_MAX": nm})
        try:
            vv.deposit(10 ** 22, w.block, w.ts())
            got = None
        except R.Revert as ex:
            got = ex.err
        check(got == want, "NAV_MAX is inclusive", (nm - cap, got))


def main():
    rng = random.Random(20261115)
    for fn in (props_ledger, props_mint, props_bank, props_drip, headroom_exact, props_placement):
        _run(fn, rng)
    for fn in (rules_med9, props_saturation, props_ladder, props_units, rules_pay, rules_gate_leg, rules_clock, rules_actions,
               rules_r2, rules_r3, rules_r4, rules_r4b, rules_r5, rules_r6, rules_r7, rules_r7b, rules_r7c, rules_r7d, rules_r8, scenario_base_and_vesting, scenario_live_leg, scenario_no_read_when_not_vestable,
               scenario_breach_and_removal, scenario_reset_and_registration, scenario_gap, scenario_actions,
               scenario_deltamin, scenario_insolvent, scenario_epilogue, rules_markout):
        _run(fn)
    print(f"SUMMARY named_failures={len(FAILS)} crashes={len(CRASHES)} checks={N_CHECKS[0]}")
    if FAILS or CRASHES:
        print(f"SELF-CHECK FAILED: {len(FAILS)} of {N_CHECKS[0]} checks; {len(CRASHES)} crashes")
        seen = {}
        for f in FAILS + CRASHES:
            k = f.split(":")[0]
            seen[k] = seen.get(k, 0) + 1
            if seen[k] <= 2:
                print("  -", f[:900])
        return 1
    print(f"self-check OK ({N_CHECKS[0]} checks)")
    return 0
