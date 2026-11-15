#!/usr/bin/env python3
"""Standing check for the reference model (run from the repository root):
    python3 model/mutants_model.py [-j N]

Each mutant breaks one rule clause in `reference.py`. A mutant is KILLED when the mutated model either
(a) fails a named self-check assertion (an unexpected revert of a legal call counts; a Python crash does not),
(b) trips a generator assertion in `vectors.py`, or (c) regenerates golden vectors that differ from the committed
ones (the vectors are the killers of the Solidity libraries). A crash-only result is INVALID, and the gate fails.

Gate (100%): every mutant killed, every rule in RULES has a killed mutant, and every clause of the curated
mutation list (CLAUSES) maps to killed mutants or to a recorded reason (n/a for Solidity-only clauses, or an
equivalence argument).
"""
import filecmp
import os
import shutil
import subprocess
import sys
import tempfile
from concurrent.futures import ThreadPoolExecutor

HERE = os.path.dirname(os.path.abspath(__file__))
VEC = os.path.join(os.path.dirname(HERE), "test", "vectors")

RULES = {
    "ledger": "worse-of charge over {spot, reference}, 0 floor, p*, one-price burn and gate",
    "fence": "|spot − reference| <= REF_BAND on every curator action, two-sided",
    "base": "base fee: δ-weighted, observed in-range blocks, eligibility, deferral",
    "bank": "cohorts, youngest-first burn, bond, vesting after V epochs, drip, release delay",
    "gate": "vest gate: fresh cache in range, no gap, live leg read once only when vestable, maturedPending",
    "snapshot": "snapshot steps 1-6: truncation, gap flag, streak on consecutive anchors, one refresh per anchor",
    "clock": "breach clock and latch per CURE_BLOCKS (block and τ_v at the latch), EMPTY counted",
    "pending": "pending breaches processed first: forfeit unvested and baseOwed, count all, ack",
    "registration": "registration writes the live tick, clears the gap, starts a run",
    "modes": "mode machine incl. the gate after the prologue and exitBlock on transition only",
    "placement": "Placement.check/size: guards on both sides, headroom, δ_after, saturating legs",
    "caps": "swap caps per action, per epoch, MAX_SWAPS, idle, limit on the adverse side, counters",
    "epilogue": "epilogue re-bases (actions at the live reference, flows NAV0·S'/S), collect-first and collect-again",
    "rounding": "rounding directions and the NAV cap in shares",
    "supply": "share supply conservation",
    "seed": "seed: armed clock, successful refresh, BOND_MIN at REF_SQRT_PRICE, dripBlocks and releaseDelay",
    "records": "epoch roll: end of the previous epoch, a stale record for a skipped gap",
    "relational": "every comparison in the model at its exact boundary (generated < <-> <=, > <-> >=)",
}

# (rule, name, old, new): `old` must occur exactly once in reference.py
MUTANTS = [
    ("bank", "oldest-first burn", "for c in sorted(b.cohorts, key=lambda c: -c[0]):", "for c in sorted(b.cohorts, key=lambda c: c[0]):"),
    ("gate", "live leg ignored", "gate_open = ok and in_rng(lo, hi, t)", "gate_open = True"),
    ("gate", "live read when not vestable", "        if gate and vestable:", "        if gate:"),
    ("gate", "cached-range leg dropped", ' and not o["gapSinceVerify"] and in_rng(lo, hi, o["refTick"])', ' and not o["gapSinceVerify"]'),
    ("gate", "freshness leg dropped", 'gate = o["refFresh"] and not o["emptyRange"]', 'gate = not o["emptyRange"]'),
    ("gate", "gap leg dropped", 'and not o["emptyRange"] and not o["gapSinceVerify"] and', 'and not o["emptyRange"] and'),
    ("gate", "vestable ignores maturedPending", "vestable = b.maturedPending > 0 or any(", "vestable = any("),
    ("gate", "maturedPending never vests", "            vested += b.maturedPending\n", ""),
    ("gate", "tryRef returns the cache", "    def try_ref(self, ts):\n        return self.ref.read(ts, 0)", "    def try_ref(self, ts):\n        return True, self.refTick"),
    ("bank", "overwrite vests while the gate is closed", "            if gate:\n                b.vestedUnclaimed = sat(b.vestedUnclaimed + slot[1], U128)",
     "            if True:\n                b.vestedUnclaimed = sat(b.vestedUnclaimed + slot[1], U128)"),
    ("ledger", "worse-of max -> min", "if best is None or loss > best[0]:", "if best is None or loss < best[0]:"),
    ("ledger", "0 floor removed", "if loss > 0 else 0", "if loss > 0 else loss"),
    ("ledger", "reference leg dropped", "    for p in (sqrt_spot, sqrt_ref):", "    for p in (sqrt_spot,):"),
    ("ledger", "reference-only", "    for p in (sqrt_spot, sqrt_ref):", "    for p in (sqrt_ref,):"),
    ("ledger", "100% -> phi", "    charge = loss if loss > 0 else 0", "    charge = loss // 10 if loss > 0 else 0"),
    ("ledger", "no-op charged", "    charge = loss if loss > 0 else 0", "    charge = loss + 1 if loss >= 0 else 0"),
    ("ledger", "gate removed in measure", "    return charge, p_star, nav_after, burn, burn <= unvested + bond", "    return charge, p_star, nav_after, burn, True"),
    ("ledger", "burn priced at spot", "        burn = ceil_div(charge * S, nav_after)", "        burn = ceil_div(charge * S, value(after, sqrt_spot)[0])"),
    ("ledger", "bond burn reported as bank", "return burn, 0, bank_burn, need", "return burn, 0, bank_burn + need, 0"),
    ("rounding", "burn rounds down", "burn = ceil_div(charge * S, nav_after)", "burn = charge * S // nav_after"),
    ("snapshot", "RESET_RUN = 1", "if self.resetStreak >= self.t.RESET_RUN:", "if self.resetStreak >= 1:"),
    ("snapshot", "gap flag never set", "        if now > self.refCachedAt + 2 * G:", "        if False:"),
    ("snapshot", "gap flag at >=", "        if now > self.refCachedAt + 2 * G:", "        if now >= self.refCachedAt + 2 * G:"),
    ("snapshot", "streak ignores consecutive anchors", "if self.refGrid + 1 == k else 1", "if True else 1"),
    ("snapshot", "out-of-range sample keeps the streak", "        else:\n            self.resetStreak = 0\n        self._write_cache(t, k, now)",
     "        else:\n            pass\n        self._write_cache(t, k, now)"),
    ("snapshot", "RESET_RUN leaves the gap set", "            self.outBlocks = 0\n            self.gapSinceVerify = False\n            self.events.append((\"GapCleared\", 1))",
     "            self.outBlocks = 0\n            self.events.append((\"GapCleared\", 1))"),
    ("snapshot", "refresh twice per anchor (snapshot path)", "if k > self.refGrid else None", "if k >= self.refGrid else None"),
    ("snapshot", "refresh twice per anchor (_refresh)", "        if k > self.refGrid:\n            ok, t", "        if k >= self.refGrid:\n            ok, t"),
    ("snapshot", "no staleness truncation", "        trunc = max(0, min(now, self.refCachedAt + 2 * G) - last)", "        trunc = max(0, now - last)"),
    ("snapshot", "refresh not boundary-anchored", "rd = self.ref.read(ts, ts - k * self.t.GRID_SEC)", "rd = self.ref.read(ts, 0)"),
    ("snapshot", "span credited with the new sample", "                    and in_rng(lo, hi, self.refTick))",
     "                    and in_rng(lo, hi, rd[1] if rd and rd[0] else self.refTick))"),
    ("snapshot", "τ(B) interpolation removed", "            cr = max(0, min(min(B, self.refCachedAt + 2 * G) - last, trunc))", "            cr = trunc"),
    ("snapshot", "refreshRef without closing the span", "        self.snapshot(now, ts, sqrt_price, tick)\n        self.refresh(now, ts)", "        self.refresh(now, ts)"),
    ("snapshot", "snapshot-path refresh not fail-soft", "        if not ok:\n            self.events.append((\"RefReadFailed\", k))\n            return",
     "        if not ok:\n            raise Revert(\"RefUnreadable\")"),
    ("base", "τ_v at the edge", "get_sqrt_price_at_tick(lo) < sqrt_price < get_sqrt_price_at_tick(hi)", "get_sqrt_price_at_tick(lo) <= sqrt_price < get_sqrt_price_at_tick(hi)"),
    ("base", "τ_v without the reference check", "                    and in_rng(lo, hi, self.refTick))", "                    )"),
    ("base", "deferral dropped", "                self.baseOwed = amount\n", "                pass\n"),
    ("base", "baseOwed minted twice", "                    self.baseOwed = 0\n                    minted = True", "                    minted = True"),
    ("base", "always mint at tryRef", '            if o["refFresh"]:\n                price = o["refTick"]', '            if False:\n                price = o["refTick"]'),
    ("base", "base accrues in EXITED", "return self.bank.placedByCurator and self.bank.mode in (NORMAL, BREACHED)", "return True"),
    ("clock", "outBlocks untruncated", "self.outBlocks = sat(self.outBlocks + trunc, U64)", "self.outBlocks = sat(self.outBlocks + (now - last), U64)"),
    ("clock", "clock on the pool tick", "        if not in_rng(lo, hi, self.refTick):\n            self.outBlocks", "        if not in_rng(lo, hi, tick):\n            self.outBlocks"),
    ("clock", "latch block wrong", "self.lastPendingBlock = self.lastSnap + (j * C - old)", "self.lastPendingBlock = self.lastSnap"),
    ("clock", "CURE >= -> >", "        while j * C <= new and n < 3:", "        while j * C < new and n < 3:"),
    ("clock", "one latch per run", "        while j * C <= new and n < 3:", "        while j * C <= new and n < 3 and old < C:"),
    ("clock", "cutoff at the first pending block", "            self.lastPendingBlock = self.lastSnap + (j * C - old)",
     "            self.lastPendingBlock = self.lastPendingBlock if self.pendingBreaches > 1 else self.lastSnap + (j * C - old)"),
    ("clock", "EMPTY not counted", "        if not in_rng(lo, hi, self.refTick):", "        if not in_rng(lo, hi, self.refTick) and not self.emptyRange:"),
    ("clock", "clock not armed at seed", "        self.armed = True\n", ""),
    ("pending", "baseOwed kept at a breach", '            r["forfeitValue"] += self.baseOwed\n            self.baseOwed = 0', '            r["forfeitValue"] += self.baseOwed'),
    ("pending", "baseOwed kept at retire", "            fv = self.baseOwed\n            self.baseOwed = 0", "            fv = self.baseOwed"),
    ("pending", "ackBreaches skipped", "            self.hook.ack_breaches(n)", "            pass"),
    ("pending", "pre-breach base minted", '            self.tauV0 = o["tauVAtLastPending"]\n', ""),
    ("pending", "single breach counted", '            n = o["pendingBreaches"]', "            n = 1"),
    ("pending", "vest before breach processing", "        # (2) drip\n", "        fee_bank_apply(b, \"VestTime\", dict(nowEpoch=ne, gateOpen=gate_open))\n        # (2) drip\n"),
    ("pending", "maturedPending not forfeited", "    new = b.cohort_total() + b.maturedPending\n", "    new = b.cohort_total()\n    b.vestedUnclaimed += b.maturedPending\n"),
    ("pending", "actions skip the checkpoint", "        o, (lr, lt) = self.prologue(now, ts)\n", "        o = self._snapshot(now, ts)\n        self._collect_first()\n        lr, lt = False, 0\n"),
    ("bank", "release delay from dripBlocks", "if now < b.exitBlock + b.releaseDelay:", "if now < b.exitBlock + b.dripBlocks:"),
    ("bank", "release outside EXITED", "        if b.mode != EXITED:\n            raise Revert(\"BadMode\")\n        if now < b.exitBlock", "        if now < b.exitBlock"),
    ("bank", "vest one epoch early", "if c[1] > 0 and ne >= c[0] + 1 + V:", "if c[1] > 0 and ne >= c[0] + V:"),
    ("bank", "drip burns all at once", "    if b.dripBlocks == 0:", "    if True:"),
    ("bank", "ring overwrite merges occupant", "        if slot[1] > 0 and slot[0] != e:", "        if False:"),
    ("bank", "drip restarted by an empty forfeit", "    if new > 0:\n        b.forfeitPool", "    if True:\n        b.forfeitPool"),
    ("bank", "drip restart drops the remainder", "        b.forfeitTotal = b.forfeitPool\n", "        b.forfeitTotal = new\n"),
    ("bank", "no drip restart", "        b.forfeitStart = now\n    return new", "    return new"),
    ("bank", "charge skips maturedPending", "        x = min(need, b.maturedPending)", "        x = 0"),
    ("bank", "vested burnable", "        b.bondShares -= need\n        return burn", "        y = min(need, b.vestedUnclaimed)\n        b.vestedUnclaimed -= y\n        need -= y\n        b.bondShares -= need\n        return burn"),
    ("bank", "non-exiting breach clears placedByCurator", "        b.mode, b.breaches = mode, br\n        if exited:\n            b.exitBlock = now\n            b.placedByCurator = False\n        return burned, 0, forfeited, b.mode\n    if op == \"VestTime\":",
     "        b.mode, b.breaches = mode, br\n        b.placedByCurator = False\n        if exited:\n            b.exitBlock = now\n        return burned, 0, forfeited, b.mode\n    if op == \"VestTime\":"),
    ("bank", "retire without forfeit", "        burned = _drip_now(b, now)\n        forfeited = _forfeit_all(b, now)\n        b.mode, b.breaches = mode, br", "        burned, forfeited = 0, 0\n        b.mode, b.breaches = mode, br"),
    ("modes", "removal not reached", "            return EXITED, b, False\n        if b >= 3:", "            return BREACHED, b, False\n        if b >= 3:"),
    ("modes", "removal at 4", "        if b >= 3:", "        if b >= 4:"),
    ("modes", "breaches == 3", "        if b >= 3:", "        if b == 3:"),
    ("modes", "mode gate skipped", "        if not ladder_allowed(self.bank.mode, E[entry]):", "        if False:"),
    ("modes", "mode gate before the prologue", "        o, (lr, lt) = self.prologue(now, ts)\n        self._gate(entry)", "        self._gate(entry)\n        o, (lr, lt) = self.prologue(now, ts)"),
    ("modes", "Place from EXITED allowed", "        if mode == EXITED:\n            raise Revert(\"BadMode\")\n        return NORMAL", "        return NORMAL"),
    ("modes", "exitBlock rewritten after exit", "            return EXITED, b, False\n        if b >= 3:", "            return EXITED, b, True\n        if b >= 3:"),
    ("modes", "deposit in EXITED", '"Deposit": (1, 1, 0)', '"Deposit": (1, 1, 1)'),
    ("modes", "topUpBond in EXITED", '"TopUpBond": (1, 1, 0)', '"TopUpBond": (1, 1, 1)'),
    ("modes", "claim blocked in EXITED", '"Claim": (1, 1, 1)', '"Claim": (1, 1, 0)'),
    ("modes", "rebalanceSwap changes the mode", "            self._swap_from_idle(zfo, amount_in, tick_spot, s_ref, self.nav(s_ref))\n",
     "            self._swap_from_idle(zfo, amount_in, tick_spot, s_ref, self.nav(s_ref))\n            self.bank.mode = NORMAL\n"),
    ("modes", "rebalanceSwap resets the clock", "            charge, p_star, burn = self._charge(before, s_spot, s_ref, \"rebalanceSwap\")\n",
     "            charge, p_star, burn = self._charge(before, s_spot, s_ref, \"rebalanceSwap\")\n            self.hook.outBlocks = 0\n"),
    ("registration", "registration keeps the gap", "        self._write_cache(live_tick, ts // self.t.GRID_SEC, now)\n        self.gapSinceVerify = False",
     "        self._write_cache(live_tick, ts // self.t.GRID_SEC, now)"),
    ("registration", "registration does not write the cache", "        self._write_cache(live_tick, ts // self.t.GRID_SEC, now)\n", ""),
    ("registration", "registration ignores liveTick", "        self._write_cache(live_tick, ts // self.t.GRID_SEC, now)\n", "        self._write_cache(self.refTick, ts // self.t.GRID_SEC, now)\n"),
    ("registration", "registration does not start a run", "        self.resetStreak = 0\n        self.outBlocks = 0\n", "        self.resetStreak = 0\n"),
    ("registration", "action reads the cache", "        ref = lt if lr else self.hook.live_ref(ts)", "        ref = self.hook.refTick"),
    ("fence", "rebalanceSwap fence removed", '            raise Revert("Fence")', "            pass"),
    ("fence", "action fence signed", "        if abs(tick_spot - ref) > self.t.REF_BAND:", "        if tick_spot - ref > self.t.REF_BAND:"),
    ("fence", "fence on med9", "        if abs(tick_spot - ref) > self.t.REF_BAND:", "        if abs(o[\"med9\"] - ref) > self.t.REF_BAND:"),
    ("fence", "fail-open on an unreadable reference", "        if not ok:\n            raise Revert(\"RefUnreadable\")\n        return t", "        if not ok:\n            return self.refTick\n        return t"),
    ("fence", "cooldown removed", '            raise Revert("Cooldown")', "            pass"),
    ("placement", "placement fence signed", "    if abs(tick_spot - ref_tick) > band:", "    if tick_spot - ref_tick > band:"),
    ("placement", "width >= instead of =", "    if hi - lo != w_ticks:", "    if hi - lo < w_ticks:"),
    ("placement", "alignment removed", "    if lo % spacing != 0 or hi % spacing != 0:", "    if False:"),
    ("placement", "spot containment removed", "    if not (lo <= tick_spot < hi):\n        return 4", "    if False:\n        return 4"),
    ("placement", "reference containment <= hi", "    if not (lo <= ref_tick < hi):", "    if not (lo <= ref_tick <= hi):"),
    ("placement", "headroom removed", "head = L0 // 10 ** 9 + 1", "head = 0"),
    ("placement", "headroom without +1", "head = L0 // 10 ** 9 + 1", "head = L0 // 10 ** 9"),
    ("placement", "headroom 1e8", "head = L0 // 10 ** 9 + 1", "head = L0 // 10 ** 8 + 1"),
    ("placement", "DELTA_MIN removed", "            if d_after < t.DELTA_MIN:", "            if False:"),
    ("placement", "DELTA_MIN >= -> >", "            if d_after < t.DELTA_MIN:", "            if d_after <= t.DELTA_MIN:"),
    ("placement", "saturating leg reverts", "    return min(amount0 * inter // (sb - sa), U128)", "    r = amount0 * inter // (sb - sa)\n    assert r <= U128\n    return r"),
    ("placement", "δ_after at spot", "    return L, after, min(delta_of(after, sqrt_ref), U64)", "    return L, after, min(delta_of(after, sqrt_spot), U64)"),
    ("caps", "MAX_SWAPS removed", "if self.swapCount >= t.MAX_SWAPS:", "if False:"),
    ("caps", "per-action cap removed", "if frac > t.MAX_SWAP_ACTION or self.epochSwapFrac", "if False or self.epochSwapFrac"),
    ("caps", "epoch cap removed", "or self.epochSwapFrac + frac > t.MAX_SWAP_EPOCH:", "or False:"),
    ("caps", "counters not reset at the roll", "            self.epochSwapFrac = 0\n            self.swapCount = 0", "            self.epochSwapFrac = 0"),
    ("caps", "idle check removed", "        if amount_in > (self.h.idle0 if zfo else self.h.idle1):", "        if False:"),
    ("caps", "swap fraction rounds down", "        frac = mul_div_up(req_val, WAD, nav_ref) if nav_ref else U64", "        frac = mul_div(req_val, WAD, nav_ref) if nav_ref else U64"),
    ("caps", "counter adds the requested amount", "        fill_val = q(used, s_ref) if zfo else used", "        fill_val = q(amount_in, s_ref) if zfo else amount_in"),
    ("caps", "limit on the favourable side", "        lim_tick = tick_spot - (t.FEE_TICKS + t.BAND_SWAP) if zfo else tick_spot + (t.FEE_TICKS + t.BAND_SWAP)",
     "        lim_tick = tick_spot + (t.FEE_TICKS + t.BAND_SWAP) if zfo else tick_spot - (t.FEE_TICKS + t.BAND_SWAP)"),
    ("caps", "limit 50x wider", "        lim_tick = tick_spot - (t.FEE_TICKS + t.BAND_SWAP) if zfo else tick_spot + (t.FEE_TICKS + t.BAND_SWAP)",
     "        lim_tick = tick_spot - 50 * (t.FEE_TICKS + t.BAND_SWAP) if zfo else tick_spot + 50 * (t.FEE_TICKS + t.BAND_SWAP)"),
    ("caps", "swap cap valued at spot", "        req_val = q(amount_in, s_ref) if zfo else amount_in", "        req_val = q(amount_in, self.pool.s) if zfo else amount_in"),
    ("caps", "caps on NAV0", "            self._swap_from_idle(zfo, amount_in, tick_spot, s_ref, self.nav(s_ref))\n", "            self._swap_from_idle(zfo, amount_in, tick_spot, s_ref, self.NAV0)\n"),
    ("caps", "pre-swap bypasses the counters", "                self._swap_from_idle(pre[\"zeroForOne\"], pre[\"amountIn\"], tick_spot, s_ref, nav_ref)\n",
     "                self._swap_from_idle(pre[\"zeroForOne\"], pre[\"amountIn\"], tick_spot, s_ref, nav_ref)\n                self.swapCount -= 1\n"),
    ("ledger", "pre-swap not charged with the placement", "            L, after, d_after = placement_size(self.h, lo, hi, self.pool.s, s_ref)\n",
     "            before = self.h.copy()\n            L, after, d_after = placement_size(self.h, lo, hi, self.pool.s, s_ref)\n"),
    ("epilogue", "no collect after the vault's own swap", "        self.swapCount += 1\n        self._collect_first()", "        self.swapCount += 1"),
    ("epilogue", "collect-first removed", "        o = self._snapshot(now, ts)\n        self._collect_first()\n", "        o = self._snapshot(now, ts)\n"),
    ("epilogue", "rebalanceSwap epilogue missing", "            self.lastAction = now\n            self.NAV0, self.delta0 = self.nav(s_ref), delta_of(self.h, s_ref)\n            return charge, p_star, burn\n",
     "            self.lastAction = now\n            return charge, p_star, burn\n"),
    ("epilogue", "setPositions epilogue at spot", "            self.NAV0, self.delta0 = self.nav(s_ref), delta_of(self.h, s_ref)\n            return charge, p_star, burn, d_after",
     "            self.NAV0, self.delta0 = self.nav(s_spot), delta_of(self.h, s_spot)\n            return charge, p_star, burn, d_after"),
    ("epilogue", "withdraw re-base missing", "            self.h.idle1 -= o1\n            self._rebase_flow(S)", "            self.h.idle1 -= o1"),
    ("epilogue", "flow epilogue uses a price", "            self.NAV0 = mul_div(self.NAV0, self.S, S_before)", "            self.NAV0 = self.nav(get_sqrt_price_at_tick(self.hook.refTick))"),
    ("epilogue", "no checkpoint on deposit", "            self.prologue(now, ts)\n            self._gate(\"Deposit\")", "            self._snapshot(now, ts)\n            self._gate(\"Deposit\")"),
    ("epilogue", "snapshot skipped before a no-swap action", "        o = self._snapshot(now, ts)\n        self._collect_first()\n        return o",
     "        o = self.hook.obs(now, self.pool.tick)\n        self._collect_first()\n        return o"),
    ("rounding", "NAV cap removed", "if self.S * self.t.BOND_RATIO > self.bank.bondShares * WAD:", "if False:"),
    ("rounding", "NAV cap inverted", "if self.S * self.t.BOND_RATIO > self.bank.bondShares * WAD:", "if self.S * self.t.BOND_RATIO < self.bank.bondShares * WAD:"),
    ("rounding", "deposit rounds down", "ceil_div(self.h.idle0 * shares, S), ceil_div(self.h.idle1 * shares, S)", "self.h.idle0 * shares // S, self.h.idle1 * shares // S"),
    ("rounding", "withdraw rounds up", "            o0, o1, dL = self.h.idle0 * shares // S, self.h.idle1 * shares // S, self.h.L * shares // S",
     "            o0, o1, dL = ceil_div(self.h.idle0 * shares, S), ceil_div(self.h.idle1 * shares, S), self.h.L * shares // S"),
    ("rounding", "withdraw with the post-burn supply", "            o0, o1, dL = self.h.idle0 * shares // S, self.h.idle1 * shares // S, self.h.L * shares // S",
     "            o0, o1, dL = self.h.idle0 * shares // (S - shares), self.h.idle1 * shares // (S - shares), self.h.L * shares // (S - shares)"),
    ("rounding", "saturate -> revert", "def sat(x, cap):\n    return 0 if x < 0 else (cap if x > cap else x)",
     "def sat(x, cap):\n    if x < 0 or x > cap:\n        raise Revert(\"Overflow\")\n    return x"),
    ("supply", "charge burn not removed from supply", "        self.S -= bb\n", "\n"),
    ("supply", "drip burn not removed from supply", "        burned, _, _, _ = fee_bank_apply(b, \"Drip\", dict(now=now))\n        self.S -= burned\n",
     "        burned, _, _, _ = fee_bank_apply(b, \"Drip\", dict(now=now))\n"),
    ("seed", "seed without a successful refresh", "        if self.refCachedAt != now:\n            raise Revert(\"RefUnreadable\")", "        pass"),
    ("seed", "BOND_MIN at the pool price", "            nav_ref = self.nav(self.REF_SQRT_PRICE)", "            nav_ref = self.nav(self.pool.s)"),
    ("seed", "no BOND_MIN check", "                raise Revert(\"BondMin\")", "                pass"),
    ("seed", "dripBlocks unset at seed", "        self.bank.dripBlocks = self.t.FORFEIT_DRIP_BLOCKS\n", ""),
    ("base", "segment not closed", '        self.tauV0 = o["tauV"]\n        self.segStart = now', "        self.segStart = now"),
    ("base", "mint at spot", "                navp = self.nav(get_sqrt_price_at_tick(price))", "                navp = self.nav(self.pool.s)"),
    ("clock", "hook in-range includes hi", "def in_rng(lo, hi, t):\n    return lo <= t < hi", "def in_rng(lo, hi, t):\n    return lo <= t <= hi"),
    ("clock", "hook in-range excludes lo", "def in_rng(lo, hi, t):\n    return lo <= t < hi", "def in_rng(lo, hi, t):\n    return lo < t < hi"),
    ("snapshot", "anchor on the block grid", "ts // self.t.GRID_SEC", "now // self.t.GRID", "all"),
    ("records", "stale record dropped", '                self.rec(self.epoch + 1)["flags"].add("stale")', "                pass"),
    ("records", "stale record when nothing is skipped", "            if ne > self.epoch + 1:", "            if ne >= self.epoch + 1:"),
    ("records", "end of epoch not written", '            self.rec(self.epoch)["end"] = now\n', ""),
    ("bank", "claim pays maturedPending", "        x = b.vestedUnclaimed\n        b.vestedUnclaimed = 0",
     "        x = b.vestedUnclaimed + b.maturedPending\n        b.vestedUnclaimed = 0\n        b.maturedPending = 0"),
]


def relational_mutants(src):
    """Generated boundary mutants: every <, <=, >, >= token in reference.py outside the Pool test double."""
    import io
    import tokenize
    swap = {"<": "<=", "<=": "<", ">": ">=", ">=": ">"}
    lines = src.splitlines(keepends=True)
    pool_start = next(i for i, l in enumerate(lines) if l.startswith("class Pool"))
    pool_end = next(i for i, l in enumerate(lines) if i > pool_start and l.startswith("# ===="))
    out = []
    for tok in tokenize.generate_tokens(io.StringIO(src).readline):
        if tok.type == tokenize.OP and tok.string in swap:
            row, col = tok.start
            if pool_start <= row - 1 < pool_end:
                continue
            line = lines[row - 1]
            new_line = line[:col] + swap[tok.string] + line[col + len(tok.string):]
            nth = sum(1 for l in lines[:row - 1] if l.strip() == line.strip())
            key = f"{line.strip()} |{tok.string}@{col}" + (f" #{nth + 1}" if nth else "")
            mutated = "".join(lines[:row - 1]) + new_line + "".join(lines[row:])
            out.append(("relational", key, mutated))
    return out


# Relational mutants proven equivalent (keyed by the stripped source line, the operator and its column).
# Each reason states why the two operators agree on every reachable input.
EQUIVALENT = {
    'return 0 if x < 0 else (cap if x > cap else x) |<@18': 'x = 0 returns 0 on both branches',
    'return 0 if x < 0 else (cap if x > cap else x) |>@37': 'x = cap returns cap on both branches',
    'assert self.CURE_BLOCKS >= self.EDGE_DELAY + 2 * self.GRID + self.REF_WINDOW // self.BLOCKTIME + self.GRID |>=@32': 'tier constructor check; no tier sits at equality (TEST 200 vs 130, DEMO 1800 vs 1500, MAINNET 7200 vs 1250)',
    'if s <= U128: |<=@9': 'at s = 2^128-1 the two quote forms differ only if x >= 2^129 and x = 1 mod 2^63, outside uint128 holdings',
    'if s <= sa: |<=@9': 's = sa: the in-range branch gives getAmount0Delta(sa, sb) and getAmount1Delta(sa, sa) = 0, the same amounts',
    'if s >= sb: |>=@9': 's = sb: the in-range branch gives getAmount0Delta(sb, sb) = 0 and getAmount1Delta(sa, sb), the same amounts',
    'charge = loss if loss > 0 else 0 |>@26': 'loss = 0 gives charge 0 on both branches',
    'if sa > sb: |>@10': 'sa = sb: swapping equal values is a no-op',
    'if sa > sb: |>@10 #2': 'sa = sb: swapping equal values is a no-op',
    'L = L0 - head if L0 > head else 0 |>@24': 'L0 = head gives L = 0 on both branches',
    'elif sqrt_spot <= sa: |<=@19': 'spot = sa: the in-range branch gives getAmount0Delta(sa, sb, L, up) and 0, the same add amounts',
    'elif sqrt_spot >= sb: |>=@19': 'spot = sb: the in-range branch gives 0 and getAmount1Delta(sa, sb, L, up), the same add amounts',
    'el = now - b.forfeitStart if now > b.forfeitStart else 0 |>@41': 'now = forfeitStart gives el = 0 on both branches',
    'if slot[1] > 0 and slot[0] != e: |>@19': 'an empty foreign slot: the overwrite moves 0 shares and rewrites the epoch, as the fall-through does',
    'if c[1] > 0 and ne >= c[0] + 1 + V: |>@20': 'a matured empty cohort: vesting or moving 0 shares changes nothing',
    'while j * C <= new and n < 3: |<@33': 'at most one crossing per call: a span credits <= 2·GRID < CURE_BLOCKS (tier check)',
    'if len(self.ring) > N_SNAP: |>@26': 'the ring keeps 31 instead of 32 entries; only the last K_MED = 9 are read',
    'if B <= last: |<=@17': 'unreachable: bmin >= floor(last/GRID)·GRID + GRID > last',
    'if self.NAV0 == 0 or dtau <= 0: |<=@34': 'dtau = 0 gives base = mulDiv(·, δ·0, WAD) = 0 on both branches',
    'if navp > amount: |>@24': "unreachable: base <= m_perBlock·NAV0·Δτ/WAD is far below NAV' (navp = amount would divide by zero)",
    'if S_before > 0: |>@20': 'unreachable: flows run only after seed (S >= S_SEED)',
    'if self.h.L > 0 and dL > 0: |>@24': 'L = 0 forces dL = ceilDiv(0, S) = 0, so the conjunction is false on both sides',
    'if self.h.L > 0 and dL > 0: |>@35': 'with L > 0 and shares > 0, dL = ceilDiv(L·s, S) >= 1, so dL = 0 is unreachable',
    'if dL > 0: |>@18': "dL = 0 with L > 0: the zero modify only collects fees, which the same call's prologue already swept",
}

# Every clause of the curated mutation list -> killed mutants, or a recorded reason.
CLAUSES = [
    ("worse-of max → min", ["worse-of max -> min"]), ("reference-only", ["reference-only"]),
    ("remove the 0 floor", ["0 floor removed"]), ("100% → φ", ["100% -> phi"]),
    ("remove the gate", ["gate removed in measure"]), ("gate and burn at different prices", ["burn priced at spot"]),
    ("charge between actions ≠ 0", ["no-op charged"]), ("collect-first removed", ["collect-first removed"]),
    ("no collect after the vault's own swap", ["no collect after the vault's own swap"]),
    ("fence removed, signed, or on med9", ["rebalanceSwap fence removed", "action fence signed", "fence on med9", "placement fence signed"]),
    ("placement-contains-spot-and-reference removed", ["spot containment removed", "reference containment <= hi"]),
    ("width rule ≥ instead of =", ["width >= instead of ="]), ("in-range ≤ tickUpper", ["reference containment <= hi", "hook in-range includes hi", "hook in-range excludes lo"]),
    ("cooldown removed", ["cooldown removed"]), ("MAX_SWAPS removed", ["MAX_SWAPS removed"]),
    ("deposit floor instead of ceil", ["deposit rounds down"]),
    ("withdraw with the post-burn supply", ["withdraw with the post-burn supply"]),
    ("L headroom removed", ["headroom removed", "headroom without +1", "headroom 1e8"]),
    ("NAV cap inverted", ["NAV cap inverted"]), ("swap limit on the favourable side", ["limit on the favourable side"]),
    ("rebalanceSwap changes the mode", ["rebalanceSwap changes the mode"]),
    ("per-action and per-epoch swap caps removed", ["per-action cap removed", "epoch cap removed"]),
    ("NAV cap removed", ["NAV cap removed"]),
    ("pool-id check removed", "n/a: Solidity access control on hook callbacks; the model has no pool key"),
    ("onlyVault removed", "n/a: Solidity access control; the model calls the hook only from the vault"),
    ("initialize/donate revert removed", "n/a: hook callbacks not modelled"),
    ("snapshot skipped before a no-swap action", ["snapshot skipped before a no-swap action"]),
    ("oldest-first burn", ["oldest-first burn"]), ("vested burnable", ["vested burnable"]),
    ("vest before breach processing", ["vest before breach processing"]),
    ("drip → instant burn", ["drip burns all at once"]), ("forfeit not to depositors", ["drip burn not removed from supply"]),
    ("δ cap removed", "equivalent: posValue <= NAV by construction (idle >= 0, floors are monotone), so min(WAD, ·) never binds"),
    ("base accrues in EXITED", ["base accrues in EXITED"]), ("τ_v without the reference check", ["τ_v without the reference check"]),
    ("NAV₀ read before settlement", "n/a: the model has no settlement step (holdings are storage); epilogue NAV₀ is covered by the epilogue mutants"),
    ("no checkpoint on deposit", ["no checkpoint on deposit"]), ("saturate → revert", ["saturate -> revert"]),
    ("CURE ≥ → >", ["CURE >= -> >"]), ("clock on the pool tick", ["clock on the pool tick"]),
    ("no staleness truncation of the cached reference", ["no staleness truncation", "outBlocks untruncated"]),
    ("span credited with post-insert state", ["span credited with the new sample"]),
    ("registration does not start a run", ["registration does not start a run"]),
    ("DELTA_MIN ≥ → >", ["DELTA_MIN >= -> >"]), ("deposit or topUpBond allowed in EXITED", ["deposit in EXITED", "topUpBond in EXITED"]),
    ("releaseBond outside EXITED or before exitBlock + V·EPOCH", ["release outside EXITED", "release delay from dripBlocks"]),
    ("retire without forfeit", ["retire without forfeit"]), ("removal at 4", ["removal at 4"]),
    ("breaches processed only by poke", ["actions skip the checkpoint"]),
    ("pending breaches processed after accrual", ["pre-breach base minted"]),
    ("base up to b_last minted instead of forfeited", ["pre-breach base minted"]),
    ("cutoff at the first instead of the last pending block", ["cutoff at the first pending block"]),
    ("breaches ≥ 3 → == 3", ["breaches == 3"]),
    ("vesting by time without a checkpoint", ["claim pays maturedPending"]),
    ("no epilogue re-base", ["rebalanceSwap epilogue missing", "withdraw re-base missing", "setPositions epilogue at spot"]),
    ("cohort-slot overwrite without vesting", ["ring overwrite merges occupant"]),
    ("τ(B) interpolation removed", ["τ(B) interpolation removed"]),
    ("snapshot steps reordered", ["span credited with the new sample", "refreshRef without closing the span"]),
    ("latch on read instead of crossing", ["CURE >= -> >", "latch block wrong"]),
    ("reset on any action", ["rebalanceSwap resets the clock"]), ("reset without RESET_RUN", ["RESET_RUN = 1"]),
    ("DELTA_MIN removed", ["DELTA_MIN removed"]), ("removal flag ignored", ["mode gate skipped"]),
    ("claim blocked in EXITED", ["claim blocked in EXITED"]),
    ("constant salt", "n/a: witness positions are not modelled (disclosure only)"),
    ("witness reserve counted in NAV", "n/a: witness not modelled"), ("roll on the withdraw path", "n/a: witness not modelled"),
    ("RESET_RUN leaves the gap set", ["RESET_RUN leaves the gap set"]), ("gate ignores gapSinceVerify", ["gap leg dropped"]),
    ("gate ignores liveIn", ["live leg ignored"]), ("exitBlock rewritten by a post-exit breach", ["exitBlock rewritten after exit"]),
    ("release delay hard-coded or taken from dripBlocks", ["release delay from dripBlocks"]),
    ("mode gate before the prologue only", ["mode gate before the prologue"]),
    ("dripBlocks unset at seed", ["dripBlocks unset at seed"]),
    ("bundler function without onlyOwner", "n/a: the bundler is periphery Solidity"),
    ("_tryRef returns the cache instead of reading", ["tryRef returns the cache"]),
    ("EMPTY not counted as out of range", ["EMPTY not counted"]),
    ("vestable ignores maturedPending", ["vestable ignores maturedPending"]),
    ("gap set at ≥ instead of >", ["gap flag at >="]), ("baseOwed survives a breach", ["baseOwed kept at a breach"]),
    ("registration does not clear the gap", ["registration keeps the gap"]),
    ("maturedPending never vests", ["maturedPending never vests"]),
    ("baseOwed minted twice or never", ["baseOwed minted twice", "deferral dropped"]),
    ("drip restarted by an empty forfeit", ["drip restarted by an empty forfeit"]),
    ("credit beyond the freshness window", ["no staleness truncation"]),
    ("gapSinceVerify not set by a stale span", ["gap flag never set"]),
    ("gapSinceVerify cleared by a single in-range sample", ["RESET_RUN = 1"]),
    ("charge burn skips maturedPending", ["charge skips maturedPending"]),
    ("retire keeps maturedPending", ["maturedPending not forfeited"]),
    ("registration clears pendingBreaches", "equivalent: every registration follows the action's own prologue, which has "
     "already processed and acknowledged every pending breach of the call (no latch can occur in between)"),
    ("ackBreaches clears instead of subtracting", "equivalent: the checkpoint acknowledges exactly the count it read "
     "after the call's snapshot, and no latch can occur between the read and the ack"),
    ("τ_v counts a price exactly on the edge", ["τ_v at the edge"]), ("vest on a stale cache", ["freshness leg dropped"]),
    ("overwrite vests while the gate is closed", ["overwrite vests while the gate is closed"]),
    ("maturedPending not forfeited by a breach", ["maturedPending not forfeited"]),
    ("non-curator epilogue uses a price", ["flow epilogue uses a price"]),
    ("deferred base not forfeited by a breach", ["baseOwed kept at a breach"]),
    ("pre-swap bypasses the swap caps or counters", ["pre-swap bypasses the counters"]),
    ("setVaultRange ignores liveTick", ["registration ignores liveTick"]),
    ("REFRESH_GAS_MIN removed", "n/a: gas is not modelled"),
    ("_readRef rounds toward 0", "n/a: the model's reference double returns ticks; `observe` arithmetic is Solidity (U-ref-fail)"),
    ("no length check", "n/a: return-data handling is Solidity"), ("return data not fixed-size", "n/a: Solidity"),
    ("no cardinality check", "n/a: constructor check, Solidity"),
    ("gate on the block grid instead of the anchor", ["anchor on the block grid"]),
    ("vest with the cache out of range", ["cached-range leg dropped"]),
    ("one latch per run instead of one per CURE_BLOCKS", ["one latch per run"]),
    ("action reads the cache instead of liveRef", ["action reads the cache"]),
    ("checkpoint NAV₀ or δ at spot", ["setPositions epilogue at spot"]),
    ("more than one refresh per grid", ["refresh twice per anchor (snapshot path)", "refresh twice per anchor (_refresh)"]),
    ("refresh not boundary-anchored", ["refresh not boundary-anchored"]),
    ("refreshRef without closing the span", ["refreshRef without closing the span"]),
    ("snapshot-path refresh not fail-soft", ["snapshot-path refresh not fail-soft"]),
    ("no orientation check", "n/a: constructor check, Solidity"),
    ("streak not zeroed on an out-of-range refresh", ["out-of-range sample keeps the streak"]),
    ("streak across a skipped grid", ["streak ignores consecutive anchors"]),
    ("swap cap valued at spot", ["swap cap valued at spot"]), ("BOND_MIN at the seed price", ["BOND_MIN at the pool price"]),
    ("pre-swap not charged with the placement", ["pre-swap not charged with the placement"]),
    ("clock not armed at seed", ["clock not armed at seed"]), ("pre-breach base not forfeited", ["pre-breach base minted"]),
    ("ring overwrite before breach processing", ["vest before breach processing"]),
    ("a single breach counted when two are pending", ["single breach counted"]),
    ("drip restart drops the remainder", ["drip restart drops the remainder"]),
    ("reference dropped from the worse-of", ["reference leg dropped"]),
    ("reference window = 1 block or slot0", "n/a: `observe` arguments are Solidity (K-ref, U-ref)"),
    ("fail-open on an unreadable reference", ["fail-open on an unreadable reference"]),
    ("reference address in mutable storage", "n/a: Solidity storage layout"),
    ("burn floor instead of ceil", ["burn rounds down"]), ("no BOND_MIN check", ["no BOND_MIN check"]),
    ("witness not fail-soft", "n/a: witness not modelled"), ("swap counters not reset at the epoch roll", ["counters not reset at the roll"]),
    ("epoch swap cap measured on epoch-start NAV", ["caps on NAV0"]),
    ("no ≥ 1 wei bucket rule", ["deposit rounds down"]), ("salt not bound to the sender", "n/a: deployment, Solidity"),
    ("placement need not contain spot", ["spot containment removed"]),
    ("Placement.size leg reverts on overflow", ["saturating leg reverts"]),
]


def mutate(src, m):
    if m[0] == "relational":
        return m[2], None
    rule, name, old, new = m[:4]
    replace_all = len(m) > 4 and m[4] == "all"
    if (src.count(old) < 1) if replace_all else (src.count(old) != 1):
        return None, f"pattern count {src.count(old)}"
    return src.replace(old, new), None


def run_one(src, m):
    name = m[1]
    msrc, err = mutate(src, m)
    if msrc is None:
        return name, "NOT-APPLICABLE", err
    d = tempfile.mkdtemp()
    try:
        for f in os.listdir(HERE):
            if f.endswith(".py"):
                shutil.copy(os.path.join(HERE, f), d)
        open(os.path.join(d, "reference.py"), "w").write(msrc)
        r = subprocess.run([sys.executable, "selfcheck_entry.py"] if False else [sys.executable, "-c",
                           "import selfcheck,sys; sys.exit(selfcheck.main())"], cwd=d, capture_output=True, text=True)
        out = r.stdout
        named = crashes = 0
        for line in out.splitlines():
            if line.startswith("SUMMARY"):
                kv = dict(x.split("=") for x in line.split()[1:])
                named, crashes = int(kv["named_failures"]), int(kv["crashes"])
        if "SUMMARY" not in out:
            crashes = 1
        if named:
            return name, "killed", f"self-check: {named} named failures"
        vd = os.path.join(d, "vec")
        env = dict(os.environ, VECTORS_OUT=vd)
        rv = subprocess.run([sys.executable, "-c", "import vectors; vectors.main()"], cwd=d, env=env,
                            capture_output=True, text=True)
        if rv.returncode != 0:
            if "AssertionError" in rv.stderr:
                return name, "killed", "vector generator assertion"
            return name, "INVALID", "vector generator crashed: " + rv.stderr.strip().splitlines()[-1][:120]
        for f in ("math.json", "ledger.json", "placement.json", "bank.json"):
            if not filecmp.cmp(os.path.join(vd, f), os.path.join(VEC, f), shallow=False):
                return name, "killed", f"vectors differ ({f})"
        if crashes:
            return name, "INVALID", "crash only"
        return name, "SURVIVED", ""
    finally:
        shutil.rmtree(d, ignore_errors=True)


def main():
    jobs = int(sys.argv[sys.argv.index("-j") + 1]) if "-j" in sys.argv else 8
    src = open(os.path.join(HERE, "reference.py")).read()
    allm = MUTANTS + relational_mutants(src)
    names = [m[1] for m in allm]
    assert len(names) == len(set(names)), "duplicate mutant names"
    with ThreadPoolExecutor(jobs) as ex:
        results = list(ex.map(lambda m: run_one(src, m), allm))
    status = {}
    for m, (_, st, why) in zip(allm, results):
        rule, name = m[0], m[1]
        if st == "SURVIVED" and name in EQUIVALENT:
            st, why = "equivalent", EQUIVALENT[name]
        status[name] = (rule, st)
        if st != "killed" or "-v" in sys.argv:
            print(f"{st:15s} [{rule}] {name}" + (f"  ({why})" if why else ""))
    stale = sorted(set(EQUIVALENT) - set(status))
    if stale:
        print("EQUIVALENT entries matching no mutant (update after an edit):", stale)
    bad = [n for n, (_, st) in status.items() if st not in ("killed", "equivalent")] + stale
    killed_rules = {r for r, st in status.values() if st == "killed"}
    uncovered = sorted(set(RULES) - killed_rules)
    clause_bad = []
    for clause, target in CLAUSES:
        if isinstance(target, str):
            if not (target.startswith("n/a:") or target.startswith("equivalent:")):
                clause_bad.append(f"{clause}: bad reason")
        else:
            for t in target:
                if status.get(t, (None, "MISSING"))[1] != "killed":
                    clause_bad.append(f"{clause}: mutant '{t}' {status.get(t, (None, 'MISSING'))[1]}")
    n_map = sum(1 for _, t in CLAUSES if not isinstance(t, str))
    n_eq = sum(1 for _, st in status.values() if st == "equivalent")
    n_kill = sum(1 for _, st in status.values() if st == "killed")
    print(f"\n{n_kill} killed, {n_eq} equivalent (with reasons), {len(bad)} failing, of {len(allm)} "
          f"({len(MUTANTS)} named + {len(allm) - len(MUTANTS)} generated relational); rules without a killed mutant: {uncovered or 'none'}; "
          f"curated clauses: {len(CLAUSES)} ({n_map} mapped to mutants, {len(CLAUSES) - n_map} with a recorded reason); "
          f"clause problems: {clause_bad or 'none'}")
    return 1 if bad or uncovered or clause_bad else 0


if __name__ == "__main__":
    sys.exit(main())
