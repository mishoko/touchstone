"""Self-check of the Touchstone reference model: property checks plus end-to-end scenarios at the TEST tier.

Each check asserts a rule of the specification; a failing check names it. Run:
    python3 model/reference.py --selfcheck
"""
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
                 start_block=12_000, ref_fail=None):
        self.t = tier
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
        return (self.block if b is None else b) * self.t.BLOCKTIME

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

    def place(self, lo, hi, pre=None):
        return self.v.set_positions(lo, hi, pre, now=self.block, ts=self.ts())

    def poke(self):
        return self.v.poke(self.block, self.ts())

    def deposit(self, shares):
        r = self.v.deposit(shares, self.block, self.ts())
        self.depositor += shares
        return r


def share_conservation(w):
    v, b = w.v, w.v.bank
    held = R.DEAD + w.depositor + b.cohort_total() + b.maturedPending + b.vestedUnclaimed + b.bondShares + b.forfeitPool
    return held + w.claimed if hasattr(w, "claimed") else held


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
            check(burn == -((-c * S) // n), "burn = ceilDiv(charge·S, NAV'(p*))")
            check(burn * n >= c * S and (burn - 1) * n < c * S, "burn is the least share count covering the charge")
            check(ok == (burn <= unv + bond), "gate in shares at the same price p*")
            check(n == (na_s if p == sp else na_r), "navAfterPStar is NAV_after at p*")
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
    check(R.fee_bank_apply(b3, "Drip", dict(now=5))[0] == 10, "dripBlocks 0 burns all without dividing by zero")
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
    o = w.v.hook.obs(w.block)
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
    paid = w.v.claim(w.block, w.ts())
    check(paid > 0 and w.v.bank.vestedUnclaimed == 0, "claim pays vested shares")
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
            w.depositor -= 10 ** 18 if name == "deposit" else 0
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
    got = w.v.release_bond(w.block, w.ts())
    check(got == bond and w.v.bank.bondShares == 0, "bond released at exitBlock + V·EPOCH")
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
    w.step(w.t.EPOCH * (2 + R.V))  # silence
    w.poke()
    check(w.v.hook.gapSinceVerify, "a span past the cache freshness sets gapSinceVerify")
    check(w.v.hook.tauV - tv == cached + 2 * w.t.GRID - (w.v.hook.ring[-2][0]), "span credit truncated at freshness",
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
        w.depositor -= 10 ** 18
    out = w.v.withdraw(w.depositor // 2, w.block, w.ts())
    check(out[0] > 0 and out[1] > 0, "withdraw in kind open in EXITED")
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
    amt2 = min(w2.v.nav(sqrt_at(T0)) // 100, w2.v.h.idle1)
    bond0 = w2.v.bank.bondShares + w2.v.bank.unvested()
    c, p, burn = w2.v.rebalance_swap(False, amt2, w2.block, w2.ts())
    check(c > 0 and p == sqrt_at(T0) and w2.v.bank.bondShares + w2.v.bank.unvested() == bond0 - burn,
          "fence swap charged at the reference leg; burn from bank then bond", (c, p))


def scenario_epilogue():
    w = World()
    w.place(T0 - 1000, T0 + 1000)
    n0, s0 = w.v.NAV0, w.v.S
    w.deposit(10 ** 26)
    check(w.v.NAV0 == R.mul_div(n0, w.v.S, s0), "deposit epilogue re-bases NAV0·S'/S without a price")
    d0 = w.v.delta0
    w.v.withdraw(10 ** 25, w.block, w.ts())
    check(w.v.delta0 == d0, "in-kind flows leave δ0 unchanged")


def main():
    rng = random.Random(20261115)
    props_ledger(rng)
    props_mint(rng)
    props_bank(rng)
    props_drip(rng)
    props_saturation()
    props_ladder()
    props_placement(rng)
    props_units()
    for sc in (scenario_base_and_vesting, scenario_live_leg, scenario_no_read_when_not_vestable,
               scenario_breach_and_removal, scenario_reset_and_registration, scenario_gap, scenario_actions,
               scenario_deltamin, scenario_insolvent, scenario_epilogue):
        try:
            sc()
        except Exception as ex:  # a crash is a failed check
            import traceback
            FAILS.append(f"{sc.__name__} crashed: {ex!r}\n{traceback.format_exc()}")
    if FAILS:
        print(f"SELF-CHECK FAILED: {len(FAILS)} of {N_CHECKS[0]} checks")
        seen = {}
        for f in FAILS:
            k = f.split(":")[0]
            seen[k] = seen.get(k, 0) + 1
            if seen[k] <= 2:
                print("  -", f[:900])
        return 1
    print(f"self-check OK ({N_CHECKS[0]} checks)")
    return 0
