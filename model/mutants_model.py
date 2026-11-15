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
    "deletion": "every single-line state assignment and state-changing call in the model (generated statement deletion)",
    "operand": "every operand of every arithmetic and boolean operator, and each arm of every conditional expression",
    "literal": "every integer literal ±1 in the model's functions and module-level constants (incl. min/max arguments)",
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
    ("ledger", "worse-of max -> min", "    if loss_r > loss_s:  # p* = sqrtSpot on a tie", "    if loss_r < loss_s:  # p* = sqrtSpot on a tie"),
    ("ledger", "0 floor removed", "    charge = loss if loss > 0 else 0", "    charge = loss"),
    ("ledger", "reference leg dropped", "    if loss_r > loss_s:  # p* = sqrtSpot on a tie", "    if False:"),
    ("ledger", "reference-only", "    if loss_r > loss_s:  # p* = sqrtSpot on a tie", "    if True:"),
    ("ledger", "100% -> phi", "    charge = loss if loss > 0 else 0", "    charge = loss // 10 if loss > 0 else 0"),
    ("ledger", "no-op charged", "    charge = loss if loss > 0 else 0", "    charge = loss + 1 if loss >= 0 else 0"),
    ("ledger", "gate removed in measure", "    return charge, p_star, nav_after, burn, burn <= unvested + bond", "    return charge, p_star, nav_after, burn, True"),
    ("ledger", "burn priced at spot", "        burn = ceil_div(charge * S, nav_after)", "        burn = ceil_div(charge * S, na_s)"),
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
    ("placement", "saturating leg reverts", "    return min(amount0 * inter // (sb - sa), U128)", "    r = amount0 * inter // (sb - sa)\n    if r > U128:\n        raise MathRevert('SafeCastOverflow')\n    return r"),
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
    ("base", "baseOwed never minted", "        amount = base + self.baseOwed\n", "        amount = base\n"),
    ("base", "second deferral overwrites baseOwed", "                self.baseOwed = amount\n", "                self.baseOwed = base\n"),
    ("base", "no base in BREACHED", "return self.bank.placedByCurator and self.bank.mode in (NORMAL, BREACHED)",
     "return self.bank.placedByCurator and self.bank.mode in (NORMAL,)"),
    ("ledger", "swap charge at the post-action spot", '            charge, p_star, burn = self._charge(before, s_spot, s_ref, "rebalanceSwap")',
     '            charge, p_star, burn = self._charge(before, self.pool.s, s_ref, "rebalanceSwap")'),
    ("ledger", "placement charge at the post-action spot", '            charge, p_star, burn = self._charge(before, s_spot, s_ref, "setPositions")',
     '            charge, p_star, burn = self._charge(before, self.pool.s, s_ref, "setPositions")'),
    ("placement", "sizing at the pre-action spot", "            L, after, d_after = placement_size(self.h, lo, hi, self.pool.s, s_ref)",
     "            L, after, d_after = placement_size(self.h, lo, hi, s_spot, s_ref)"),
    ("fence", "no cooldown after rebalanceSwap", "            self.lastAction = now\n            self.NAV0, self.delta0 = self.nav(s_ref), delta_of(self.h, s_ref)\n            return charge, p_star, burn\n",
     "            self.NAV0, self.delta0 = self.nav(s_ref), delta_of(self.h, s_ref)\n            return charge, p_star, burn\n"),
    ("bank", "claim pays maturedPending", "        x = b.vestedUnclaimed\n        b.vestedUnclaimed = 0",
     "        x = b.vestedUnclaimed + b.maturedPending\n        b.vestedUnclaimed = 0\n        b.maturedPending = 0"),
]


EXCLUDED_CLASSES = ("Pool", "RefSource", "Tier")  # test doubles and tier configuration (checked at construction)


def excluded_lines(src):
    import ast
    tree = ast.parse(src)
    out = set()
    for n in tree.body:
        if isinstance(n, ast.ClassDef) and n.name in EXCLUDED_CLASSES:
            out |= set(range(n.lineno, n.end_lineno + 1))
        elif not isinstance(n, (ast.FunctionDef, ast.ClassDef)):
            out |= set(range(n.lineno, n.end_lineno + 1))  # module-level configuration and the __main__ block
    return out


def _abs(blines, lineno, col):
    """Byte offset of an ast (lineno, col_offset); col_offset counts UTF-8 bytes."""
    return sum(len(l) for l in blines[:lineno - 1]) + col


def _sites(src, with_tree=False):
    """ast nodes inside the model's functions (constructors, the revert harness, test doubles and the tier
    configuration excluded), with byte spans."""
    import ast
    bsrc = src.encode()
    lines = bsrc.splitlines(keepends=True)
    tree = ast.parse(src)
    skip = excluded_lines(src)
    funcs = []
    for f in ast.walk(tree):
        if isinstance(f, ast.FunctionDef) and f.name not in ("__init__", "call") and f.lineno not in skip:
            funcs.append(f)
    ids, nodes = set(), []
    for f in funcs:
        selfful = bool(f.args.args) and f.args.args[0].arg == "self"
        for n in ast.walk(f):
            if n is f or id(n) in ids or not hasattr(n, "lineno") or n.lineno in skip:
                continue
            ids.add(id(n))
            nodes.append((n, selfful, _abs(lines, n.lineno, n.col_offset), _abs(lines, n.end_lineno, n.end_col_offset)))
    nodes.sort(key=lambda x: (x[2], -x[3]))
    return (bsrc, nodes, tree) if with_tree else (bsrc, nodes)


def _keyed(seen, key):
    seen[key] = seen.get(key, 0) + 1
    return key + (f" #{seen[key]}" if seen[key] > 1 else "")


def relational_mutants(src):
    """Generated boundary mutants: every <, <=, >, >= in a comparison, swapped with its inclusive or strict twin."""
    import ast
    swap = {ast.Lt: ("<", "<="), ast.LtE: ("<=", "<"), ast.Gt: (">", ">="), ast.GtE: (">=", ">")}
    names = {ast.Lt: "<", ast.LtE: "<=", ast.Gt: ">", ast.GtE: ">=", ast.Eq: "==", ast.NotEq: "!="}
    bsrc, nodes = _sites(src)
    out, seen = [], {}
    for n, _, a, b in nodes:
        if not isinstance(n, ast.Compare) or any(type(o) not in names for o in n.ops):
            continue
        operands = [n.left] + list(n.comparators)
        lines = bsrc.splitlines(keepends=True)
        spans = [(_abs(lines, x.lineno, x.col_offset), _abs(lines, x.end_lineno, x.end_col_offset)) for x in operands]
        for i, op in enumerate(n.ops):
            if type(op) not in swap:
                continue
            old, new = swap[type(op)]
            gap = bsrc[spans[i][1]:spans[i + 1][0]]
            k = gap.find(old.encode())
            if old == "<" and gap[k:k + 2] == b"<=" or old == ">" and gap[k:k + 2] == b">=":
                k = -1
            if k < 0:
                continue
            pos = spans[i][1] + k
            mutated = (bsrc[:pos] + new.encode() + bsrc[pos + len(old):]).decode()
            key = _keyed(seen, f"rel:{old}->{new}: {bsrc[a:b].decode()[:90]}")
            meta = ("rel", a, b, i, [names[type(o)] for o in n.ops], [bsrc[x:y].decode() for x, y in spans])
            out.append(("relational", key, mutated, meta))
    return out


def deletion_mutants(src):
    """Generated statement deletions: every state-writing statement (an assignment to an attribute or subscript, an
    augmented assignment, a call statement) replaced by `pass`. Local bindings are excluded (deleting one only
    raises NameError), and so is the event log (s2's scenario vectors pin emitted events)."""
    import ast
    bsrc, nodes = _sites(src)
    out, seen = [], {}

    def flat(e):
        return [x for y in e.elts for x in flat(y)] if isinstance(e, ast.Tuple) else [e]
    for n, selfful, a, b in nodes:
        if not isinstance(n, (ast.Assign, ast.AugAssign, ast.Expr)) or n.lineno != n.end_lineno:
            continue
        if isinstance(n, ast.Expr) and not isinstance(n.value, ast.Call):
            continue
        targets = n.targets if isinstance(n, ast.Assign) else ([n.target] if isinstance(n, ast.AugAssign) else [])
        fl = [x for t in targets for x in flat(t)]
        if targets and all(isinstance(e, ast.Name) for e in fl):
            continue
        text = bsrc[a:b].decode()
        if "events.append" in text or text.startswith(("self.ts[",)):
            continue
        mutated = (bsrc[:a] + b"pass" + bsrc[b:]).decode()
        tgt = None
        if targets:
            tgt = "(" + ", ".join(ast.get_source_segment(src, e) for e in fl if not isinstance(e, ast.Name)) + ",)"
        key = _keyed(seen, f"del: {text[:110]}")
        out.append(("deletion", key, mutated, ("del", a, b, text, tgt, selfful)))
    return out


def operand_mutants(src):
    """Generated operand mutants: replace each binary arithmetic node (+ - * //) by either operand, drop each
    operand of a boolean and/or, and fix each conditional expression to one arm."""
    import ast
    bsrc, nodes = _sites(src)
    out, seen = [], {}
    for n, _, a, b in nodes:
        repls = []
        if isinstance(n, ast.BinOp) and isinstance(n.op, (ast.Add, ast.Sub, ast.Mult, ast.FloorDiv)):
            repls = [("left", "(" + ast.get_source_segment(src, n.left) + ")"),
                     ("right", "(" + ast.get_source_segment(src, n.right) + ")")]
        elif isinstance(n, ast.BoolOp):
            op = " and " if isinstance(n.op, ast.And) else " or "
            for k in range(len(n.values)):
                rest = [v for j, v in enumerate(n.values) if j != k]
                repls.append((f"drop{k}", "(" + op.join("(" + ast.get_source_segment(src, v) + ")" for v in rest) + ")"))
        elif isinstance(n, ast.IfExp):
            repls = [("then", "(" + ast.get_source_segment(src, n.body) + ")"),
                     ("else", "(" + ast.get_source_segment(src, n.orelse) + ")")]
        orig = bsrc[a:b].decode()
        for tag, text in repls:
            key = _keyed(seen, f"op:{tag}: {orig[:90]}")
            out.append(("operand", key, (bsrc[:a] + text.encode() + bsrc[b:]).decode(),
                        ("op", a, b, orig, text, isinstance(n, ast.BoolOp))))
    return out


def literal_mutants(src):
    """Generated literal mutants: every integer literal in the model's functions and in module-level constant
    assignments, replaced by its value ±1 (test doubles excluded)."""
    import ast
    bsrc, nodes, tree = _sites(src, with_tree=True)
    lines = bsrc.splitlines(keepends=True)
    module_consts = []
    for top in tree.body:
        if isinstance(top, ast.Assign) and all(isinstance(t, ast.Name) and t.id.isupper() or isinstance(t, ast.Name)
                                               for t in top.targets):
            if any(isinstance(t, ast.Name) and t.id in ("ENTRY", "E", "OPS") for t in top.targets):
                continue
            for n in ast.walk(top):
                if hasattr(n, "lineno"):
                    module_consts.append((n, False, _abs(lines, n.lineno, n.col_offset), _abs(lines, n.end_lineno, n.end_col_offset)))
    parent = {}
    for p_ in ast.walk(tree):
        for c in ast.iter_child_nodes(p_):
            parent[id(c)] = p_
    stop = (ast.Call, ast.Lambda, ast.ListComp, ast.GeneratorExp, ast.SetComp, ast.DictComp)
    out, seen = [], {}
    for n, _, a, b in sorted(nodes + module_consts, key=lambda x: (x[2], -x[3])):
        if not (isinstance(n, ast.Constant) and type(n.value) is int):
            continue
        if isinstance(parent.get(id(n)), ast.Subscript) and parent[id(n)].slice is n:
            continue  # a struct-field index (c[1], rd[0]): field selection, not a value
        # the probe site: the largest enclosing expression below any call or comprehension boundary
        top = n
        while isinstance(parent.get(id(top)), ast.expr) and not isinstance(parent[id(top)], stop):
            top = parent[id(top)]
        pt = parent.get(id(top))
        as_bool = isinstance(pt, (ast.If, ast.While, ast.BoolOp)) or (isinstance(pt, ast.IfExp) and pt.test is top)
        A, B = _abs(lines, top.lineno, top.col_offset), _abs(lines, top.end_lineno, top.end_col_offset)
        orig = bsrc[a:b].decode()
        ctx = bsrc[max(0, bsrc.rfind(b"\n", 0, a) + 1):bsrc.find(b"\n", b)].decode().strip()[:80]
        for d in (1, -1):
            text = f"({n.value + d})"
            key = _keyed(seen, f"lit:{orig}{'+' if d > 0 else '-'}1: {ctx}")
            top_orig = bsrc[A:B].decode()
            top_mut = (bsrc[A:a] + text.encode() + bsrc[b:B]).decode()
            out.append(("literal", key, (bsrc[:a] + text.encode() + bsrc[b:]).decode(),
                        ("op", A, B, top_orig, "(" + top_mut + ")", as_bool)))
    return out


def instrument(src, meta, key):
    """The clean model with a reachability probe at one site (see probe_rt.py)."""
    bsrc = src.encode()
    kind, a, b = meta[0], meta[1], meta[2]
    k = repr(key)
    if kind == "rel":
        _, _, _, i, ops, vals = meta
        rep = f"_PR.rel({k}, {i}, {ops!r}, " + ", ".join("(" + v + ")" for v in vals) + ")"
    elif kind == "op":
        _, _, _, orig, alt, as_bool = meta
        rep = f"_PR.opnd({k}, lambda: ({orig}), lambda: {alt}, {as_bool})"
    else:
        _, _, _, text, tgt, selfful = meta
        line_start = bsrc.rfind(b"\n", 0, a) + 1
        ind = bsrc[line_start:a].decode()
        if tgt:
            rep = (f"__pb = _PR.get(lambda: {tgt}); {text}; _PR.deleted({k}, __pb, _PR.get(lambda: {tgt}))")
        else:
            sn = "_PR.snap(self)" if selfful else "None"
            rep = (f"__pb = {sn}\n{ind}try:\n{ind}    {text}\n{ind}except Exception:\n{ind}    _PR.raised({k})\n"
                   f"{ind}    raise\n{ind}_PR.deleted({k}, __pb, {sn})")
    out = (bsrc[:a] + rep.encode() + bsrc[b:]).decode()
    return "import probe_rt as _PR\n" + out


def probe_hits(src, m):
    """Run the self-check and the fuzz battery on the clean model instrumented at mutant m's site."""
    import json
    d = tempfile.mkdtemp()
    try:
        for f in os.listdir(HERE):
            if f.endswith(".py"):
                shutil.copy(os.path.join(HERE, f), d)
        open(os.path.join(d, "reference.py"), "w").write(instrument(src, m[3], m[1]))
        tot = [0, 0, 0]  # site runs, condition (all phases), condition outside the self-check
        for i, cmd in enumerate(("import selfcheck; selfcheck.main()", "import fuzz_trace; fuzz_trace.main()",
                                 "import vectors; vectors.main()")):
            out = os.path.join(d, f"probe{i}.json")
            subprocess.run([sys.executable, "-c", cmd], cwd=d,
                           env=dict(os.environ, PROBE_OUT=out, VECTORS_OUT=os.path.join(d, "vec")),
                           capture_output=True, text=True)
            if os.path.exists(out):
                h = json.load(open(out)).get(m[1], [0, 0])
                tot = [tot[0] + h[0], tot[1] + h[1], tot[2] + (h[1] if i > 0 else 0)]
        return tot
    finally:
        shutil.rmtree(d, ignore_errors=True)


def fuzz_trace_of(directory):
    r = subprocess.run([sys.executable, "-c", "import fuzz_trace; fuzz_trace.main()"], cwd=directory,
                       capture_output=True, text=True)
    return next((l for l in r.stdout.splitlines() if l.startswith("TRACE")), "TRACE-FAILED " + r.stderr[-200:])


# Relational mutants proven equivalent (keyed by the stripped source line, the operator and its column).
# Each reason states why the two operators agree on every reachable input.
# Equivalence claims. EQUIVALENT: the distinguishing condition occurs (probe hits > 0) and the outputs still match.
# UNREACHABLE: the site runs but its distinguishing condition never occurs (probe hits == 0). Both require the
# mutant to reproduce the clean randomized trace; probes run the self-check, the fuzz battery and vector generation.
EQUIVALENT = {
    'rel:<-><=: x < 0': 'x = 0 returns 0 on both branches',
    'rel:>->>=: x > cap': 'x = cap returns cap on both branches',
    'rel:<=-><: s <= sa': 's = sa: the in-range branch gives getAmount0Delta(sa, sb) and getAmount1Delta(sa, sa) = 0, the same amounts',
    'rel:>=->>: s >= sb': 's = sb: the in-range branch gives getAmount0Delta(sb, sb) = 0 and getAmount1Delta(sa, sb), the same amounts',
    'rel:>->>=: loss > 0': 'loss = 0 gives charge 0 on both branches',
    'rel:<=-><: sqrt_spot <= sa': 'spot = sa: the in-range branch gives the same add amounts (getAmount1Delta(sa, sa) = 0)',
    'rel:>=->>: sqrt_spot >= sb': 'spot = sb: the in-range branch gives the same add amounts (getAmount0Delta(sb, sb) = 0)',
    'rel:>->>=: now > b.forfeitStart': 'now = forfeitStart gives el = 0 on both branches',
    'rel:>->>=: slot[1] > 0': 'an empty foreign slot: the overwrite moves 0 shares and rewrites the epoch, as the fall-through does',
    'rel:>->>=: c[1] > 0': 'an empty matured cohort: vesting or moving 0 shares changes nothing',
    'rel:<=-><: dtau <= 0': 'dtau = 0 gives base = mulDiv(·, δ·0, WAD) = 0 on both branches',
    'rel:>->>=: self.h.L > 0 #2': 'deposit at L = 0: dL = ceilDiv(0, S) = 0, so the conjunction is false on both sides',
    'del: self.segStart = o["lastPendingBlock"]': 'dead store: step (4) of the same checkpoint sets segStart := now, and segStart feeds no computation',
    'op:drop1: T == 0 or b.forfeitPool == 0': 'pool = 0 with T > 0: the drip burns sat(so_far − T) = 0 on both paths',
    'op:drop0: slot[1] > 0 and slot[0] != e': 'an empty slot: the overwrite moves 0 shares, as the fall-through does',
    'op:drop0: c[1] > 0 and ne >= c[0] + 1 + V': 'an empty matured cohort: vesting or moving 0 shares changes nothing',
    'op:left: last // G * G + G': 'the B <= last guard skips any boundary at or before lastSnap',
    'op:right: last // G * G + G': 'the B <= last guard skips any boundary at or before lastSnap',
    'op:drop1: self.NAV0 == 0 or dtau <= 0': 'dtau = 0 gives base 0 on both paths',
    'op:drop0: self.bank.placedByCurator and self.bank.mode in (NORMAL, BREACHED)': 'not placed while NORMAL/BREACHED only before the first placement, when δ0 = 0 makes the base 0',
    'rel:<=-><: s <= U128': 's = 2^128 − 1 occurs in the math vectors; the two quote forms agree there for every x < 2^129',
    'rel:>->>=: L0 > head': 'L0 = head (L0 = 1) gives L = 0 on both branches',
    'del: self._gate("Retire")': 'the gate reverts only in EXITED, where Op.Retire (Ladder.next) reverts BadMode with the same state',
    'del: self._gate("ReleaseBond")': 'the gate reverts only outside EXITED, where Op.Release reverts BadMode with the same state',
    'op:then: now - b.forfeitStart if now > b.forfeitStart else 0': 'now < forfeitStart gives a negative target, and the burn saturates at 0, as with el = 0',
    'lit:0+1: if L == 0:': 'with L = 0 the general branches give zero amounts too (every amount is linear in L)',
    'lit:0-1: if L == 0:': 'with L = 0 the general branches give zero amounts too (every amount is linear in L)',
    'lit:0-1: if charge == 0:': "a zero charge with NAV'(p*) > 0 burns ceilDiv(0, NAV') = 0; NAV' = 0 with a zero charge needs empty holdings before and after",
    'lit:0+1: if L == 0: #2': 'with L = 0 the general branches give zero amounts too (every amount is linear in L)',
    'lit:0-1: if L == 0: #2': 'with L = 0 the general branches give zero amounts too (every amount is linear in L)',
    'lit:1+1: "Deposit": (1, 1, 0), "Withdraw": (1, 1, 1), "SetPositions": (1, 1, 0), "Rebalan': 'the entry-point table is read through bool(); 2 is truthy like 1',
    'lit:1+1: "Deposit": (1, 1, 0), "Withdraw": (1, 1, 1), "SetPositions": (1, 1, 0), "Rebalan #2': 'the entry-point table is read through bool(); 2 is truthy like 1',
    'lit:1+1: "Deposit": (1, 1, 0), "Withdraw": (1, 1, 1), "SetPositions": (1, 1, 0), "Rebalan #3': 'the entry-point table is read through bool(); 2 is truthy like 1',
    'lit:1+1: "Deposit": (1, 1, 0), "Withdraw": (1, 1, 1), "SetPositions": (1, 1, 0), "Rebalan #4': 'the entry-point table is read through bool(); 2 is truthy like 1',
    'lit:1+1: "Deposit": (1, 1, 0), "Withdraw": (1, 1, 1), "SetPositions": (1, 1, 0), "Rebalan #5': 'the entry-point table is read through bool(); 2 is truthy like 1',
    'lit:1+1: "Deposit": (1, 1, 0), "Withdraw": (1, 1, 1), "SetPositions": (1, 1, 0), "Rebalan #6': 'the entry-point table is read through bool(); 2 is truthy like 1',
    'lit:1+1: "Deposit": (1, 1, 0), "Withdraw": (1, 1, 1), "SetPositions": (1, 1, 0), "Rebalan #7': 'the entry-point table is read through bool(); 2 is truthy like 1',
    'lit:1+1: "Deposit": (1, 1, 0), "Withdraw": (1, 1, 1), "SetPositions": (1, 1, 0), "Rebalan #8': 'the entry-point table is read through bool(); 2 is truthy like 1',
    'lit:1+1: "Deposit": (1, 1, 0), "Withdraw": (1, 1, 1), "SetPositions": (1, 1, 0), "Rebalan #9': 'the entry-point table is read through bool(); 2 is truthy like 1',
    'lit:1+1: "Claim": (1, 1, 1), "TopUpBond": (1, 1, 0), "Retire": (1, 1, 0), "ReleaseBond": ': 'the entry-point table is read through bool(); 2 is truthy like 1',
    'lit:1+1: "Claim": (1, 1, 1), "TopUpBond": (1, 1, 0), "Retire": (1, 1, 0), "ReleaseBond":  #2': 'the entry-point table is read through bool(); 2 is truthy like 1',
    'lit:1+1: "Claim": (1, 1, 1), "TopUpBond": (1, 1, 0), "Retire": (1, 1, 0), "ReleaseBond":  #3': 'the entry-point table is read through bool(); 2 is truthy like 1',
    'lit:1+1: "Claim": (1, 1, 1), "TopUpBond": (1, 1, 0), "Retire": (1, 1, 0), "ReleaseBond":  #4': 'the entry-point table is read through bool(); 2 is truthy like 1',
    'lit:1+1: "Claim": (1, 1, 1), "TopUpBond": (1, 1, 0), "Retire": (1, 1, 0), "ReleaseBond":  #5': 'the entry-point table is read through bool(); 2 is truthy like 1',
    'lit:1+1: "Claim": (1, 1, 1), "TopUpBond": (1, 1, 0), "Retire": (1, 1, 0), "ReleaseBond":  #6': 'the entry-point table is read through bool(); 2 is truthy like 1',
    'lit:1+1: "Claim": (1, 1, 1), "TopUpBond": (1, 1, 0), "Retire": (1, 1, 0), "ReleaseBond":  #7': 'the entry-point table is read through bool(); 2 is truthy like 1',
    'lit:1+1: "Claim": (1, 1, 1), "TopUpBond": (1, 1, 0), "Retire": (1, 1, 0), "ReleaseBond":  #8': 'the entry-point table is read through bool(); 2 is truthy like 1',
    'lit:1+1: "Poke": (1, 1, 1), "ProcessBreach": (1, 1, 1), "Seed": (0, 0, 0),': 'the entry-point table is read through bool(); 2 is truthy like 1',
    'lit:1+1: "Poke": (1, 1, 1), "ProcessBreach": (1, 1, 1), "Seed": (0, 0, 0), #2': 'the entry-point table is read through bool(); 2 is truthy like 1',
    'lit:1+1: "Poke": (1, 1, 1), "ProcessBreach": (1, 1, 1), "Seed": (0, 0, 0), #3': 'the entry-point table is read through bool(); 2 is truthy like 1',
    'lit:1+1: "Poke": (1, 1, 1), "ProcessBreach": (1, 1, 1), "Seed": (0, 0, 0), #4': 'the entry-point table is read through bool(); 2 is truthy like 1',
    'lit:1+1: "Poke": (1, 1, 1), "ProcessBreach": (1, 1, 1), "Seed": (0, 0, 0), #5': 'the entry-point table is read through bool(); 2 is truthy like 1',
    'lit:1+1: "Poke": (1, 1, 1), "ProcessBreach": (1, 1, 1), "Seed": (0, 0, 0), #6': 'the entry-point table is read through bool(); 2 is truthy like 1',
    'lit:0-1: if T == 0 or b.forfeitPool == 0: #2': 'an empty pool with T > 0 burns sat(so_far - T) = 0 on both paths; a pool of -1 never occurs',
    'lit:0-1: el = now - b.forfeitStart if now > b.forfeitStart else 0': 'now <= forfeitStart: el = -1 gives a negative target, and the burn saturates at 0 as with el = 0',
    'lit:0-1: if slot[1] > 0 and slot[0] != e:': 'an empty foreign slot: the overwrite moves 0 shares and rewrites the epoch, as the fall-through does',
    'lit:0-1: if c[1] > 0 and ne >= c[0] + 1 + V:': 'an empty matured cohort: vesting or moving 0 shares changes nothing',
    'lit:0-1: if need == 0:': 'with need = 0 the loop continues and takes min(0, ·) = 0 from every later bucket',
    'lit:0+1: mode, br, _ = ladder_next(b.mode, b.breaches, EV_PLACE, 0)': 'the count argument is ignored for Place and Retire',
    'lit:0-1: mode, br, _ = ladder_next(b.mode, b.breaches, EV_PLACE, 0)': 'the count argument is ignored for Place and Retire',
    'lit:0+1: mode, br, exited = ladder_next(b.mode, b.breaches, EV_RETIRE, 0)  # reverts BadM': 'the count argument is ignored for Place and Retire',
    'lit:0-1: mode, br, exited = ladder_next(b.mode, b.breaches, EV_RETIRE, 0)  # reverts BadM': 'the count argument is ignored for Place and Retire',
    'lit:0+1: n = 0': 'at most one CURE_BLOCKS crossing per call (a span credits <= 2·GRID < CURE_BLOCKS), so the latch cap and stride never act',
    'lit:0-1: n = 0': 'at most one CURE_BLOCKS crossing per call (a span credits <= 2·GRID < CURE_BLOCKS), so the latch cap and stride never act',
    'lit:1+1: j += 1': 'at most one CURE_BLOCKS crossing per call (a span credits <= 2·GRID < CURE_BLOCKS), so the latch cap and stride never act',
    'lit:1+1: n += 1': 'at most one CURE_BLOCKS crossing per call (a span credits <= 2·GRID < CURE_BLOCKS), so the latch cap and stride never act',
    'lit:1-1: n += 1': 'at most one CURE_BLOCKS crossing per call (a span credits <= 2·GRID < CURE_BLOCKS), so the latch cap and stride never act',
    'lit:1+1: for B in range(bmin, bmax + 1, G):': 'boundaries advance in steps of GRID >= 2, so bmax + 1 and bmax + 2 bound the same set',
    'lit:2+1: cr = max(0, min(min(B, self.refCachedAt + 2 * G) - last, trunc))': 'past the staleness point the min with trunc caps the credit at the same value',
    'lit:0-1: if self.NAV0 == 0 or dtau <= 0: #2': 'dtau = 0 gives base 0 on both paths',
    'lit:0+1: live_read, live_tick = False, 0': 'live_tick is read only when live_read is true',
    'lit:0-1: live_read, live_tick = False, 0': 'live_tick is read only when live_read is true',
    'lit:0-1: self.h.L = 0': 'the holdings are replaced by the sized `after` holdings before L is read again',
}
UNREACHABLE = {
    'rel:>->>=: sa > sb': 'every caller passes sa < sb (W_TICKS > 0)',
    'rel:>->>=: sa > sb #2': 'every caller passes sa < sb (W_TICKS > 0)',
    'rel:<-><=: n < 3': 'at most one crossing per call: a span credits <= 2·GRID < CURE_BLOCKS',
    'rel:<=-><: B <= last': 'bmin >= floor(last/GRID)·GRID + GRID > last',
    'rel:>->>=: S_before > 0': 'flows run only after seed (S >= S_SEED)',
    'del: self.lastSnap = now #3': "arm_at_seed: seed's own snapshot already set lastSnap to now",
    'del: self._latch(self.outBlocks, self.outBlocks)': 'old == new: the latch loop cannot run',
    'del: self.S -= burned #2': "Op.Breach's drip at now burns 0: checkpoint step (2) already burned the drip owed at the same now",
    'del: self.S -= burned #3': "Op.Retire's drip at now burns 0: the retire prologue already burned the drip owed at the same now",
    'del: self.delta0 = 0': "seed: equal to the constructor's value",
    'del: self.tauV0 = self.hook.tauV': "seed: the hook's tauV is 0 at arming, equal to the constructor's value",
    'del: self.bank.mode = NORMAL': 'seed: Bank() starts in NORMAL',
    'del: self._gate("Withdraw")': 'withdraw is allowed in every mode, so the gate never reverts',
    'del: self._gate("Claim")': 'claim is allowed in every mode, so the gate never reverts',
    'op:drop0: lo % spacing != 0 or hi % spacing != 0': 'after the width check, hi − lo = W_TICKS is a multiple of spacing, so lo and hi are aligned together',
    'op:drop1: lo % spacing != 0 or hi % spacing != 0': 'after the width check, hi − lo = W_TICKS is a multiple of spacing, so lo and hi are aligned together',
    'op:drop0: a0 > idle.idle0 or a1 > idle.idle1': 'the headroom keeps both add amounts within idle',
    'op:drop1: a0 > idle.idle0 or a1 > idle.idle1': 'the headroom keeps both add amounts within idle',
    'op:drop0: T == 0 or b.forfeitPool == 0': 'forfeitPool <= T, so T = 0 implies pool = 0',
    'op:drop1: j * C <= new and n < 3': 'at most one crossing per call',
    'op:drop0: not self.emptyRange and get_sqrt_price_at_tick(lo) < sqrt_price < get_sqrt_price_at_tick(h': 'EMPTY stores lo = hi = 0, so the strict range test is already false',
    'op:drop1: self.bank.placedByCurator and self.bank.mode in (NORMAL, BREACHED)': 'placedByCurator is cleared on every transition to EXITED',
    'op:drop1: o["refFresh"] and not o["emptyRange"] and not o["gapSinceVerify"] and in_rng(lo, hi, o["re': 'EMPTY stores lo = hi = 0, so the cached-range leg is already false',
    'op:drop0: self.h.L > 0 and dL > 0': 'L > 0 implies dL = ceilDiv(L·s, S) >= 1, and L = 0 implies dL = 0',
    'op:drop1: self.h.L > 0 and dL > 0': 'L > 0 implies dL = ceilDiv(L·s, S) >= 1, and L = 0 implies dL = 0',
    'op:then: mul_div_up(req_val, WAD, nav_ref) if nav_ref else U64': 'NAV at the reference is > 0 after seed (DEAD shares hold value)',
    'op:then: mul_div_up(fill_val, WAD, nav_ref) if nav_ref else 0': 'NAV at the reference is > 0 after seed',
    'op:drop1: now == self.lastSnap and self.ring': 'the ring is non-empty whenever now == lastSnap (every snapshot inserts)',
    'rel:>->>=: dL > 0': 'deposit: L > 0 implies dL = ceilDiv(L·s, S) >= 1, so dL = 0 with L > 0 never occurs',
    'op:drop0: self.NAV0 == 0 or dtau <= 0': 'NAV0 > 0 after seed (DEAD shares hold value)',
    'lit:0+1: return 0 if x < 0 else (cap if x > cap else x) #2': 'sat(x) for x in {-1, 0, 1}: both forms return the same value',
    'lit:0-1: return 0 if x < 0 else (cap if x > cap else x) #2': 'sat(x) for x in {-1, 0, 1}: both forms return the same value',
    'lit:0-1: charge = loss if loss > 0 else 0': 'loss = 0 gives a charge of 0 either way',
    'lit:0-1: if T == 0 or b.forfeitPool == 0:': 'T = -1 never occurs; T = 0 implies an empty pool, caught by the other leg',
    'lit:3+1: while j * C <= new and n < 3:': 'at most one CURE_BLOCKS crossing per call (a span credits <= 2·GRID < CURE_BLOCKS), so the latch cap and stride never act',
    'lit:3-1: while j * C <= new and n < 3:': 'at most one CURE_BLOCKS crossing per call (a span credits <= 2·GRID < CURE_BLOCKS), so the latch cap and stride never act',
    'lit:0+1: if self.h.L > 0:': 'a position of exactly one liquidity unit never occurs (L is sized from real inventory with a headroom)',
    'lit:0+1: if self.NAV0 == 0 or dtau <= 0:': 'NAV0 in {-1, 1} never occurs (NAV0 is the vault NAV at the reference after seed)',
    'lit:0-1: if self.NAV0 == 0 or dtau <= 0:': 'NAV0 in {-1, 1} never occurs (NAV0 is the vault NAV at the reference after seed)',
    'lit:0+1: if S_before > 0:': 'S in {-1, 0, 1} never occurs for a flow (S >= S_SEED after seed)',
    'lit:0-1: if S_before > 0:': 'S in {-1, 0, 1} never occurs for a flow (S >= S_SEED after seed)',
    'lit:0+1: if self.h.L > 0 and dL > 0:': 'L > 0 implies dL = ceilDiv(L·s, S) >= 1 and L = 0 implies dL = 0, so the edge values never change the outcome',
    'lit:0-1: if self.h.L > 0 and dL > 0:': 'L > 0 implies dL = ceilDiv(L·s, S) >= 1 and L = 0 implies dL = 0, so the edge values never change the outcome',
    'lit:0-1: if self.h.L > 0 and dL > 0: #2': 'L > 0 implies dL = ceilDiv(L·s, S) >= 1 and L = 0 implies dL = 0, so the edge values never change the outcome',
    'lit:0+1: self.epochSwapFrac += mul_div_up(fill_val, WAD, nav_ref) if nav_ref else 0': 'NAV at the reference is > 0 after seed, so the else arm never runs',
    'lit:0-1: self.epochSwapFrac += mul_div_up(fill_val, WAD, nav_ref) if nav_ref else 0': 'NAV at the reference is > 0 after seed, so the else arm never runs',
    'lit:0+1: if self.h.L > 0: #2': 'a position of exactly one liquidity unit never occurs (L is sized from real inventory with a headroom)',
    'lit:0+1: if L > 0:  # settle what the pool takes for L at its current price (equal to the': 'an L = 1 placement has δ_after ≈ 0 and reverts DeltaMin, rolling the skipped add back',
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
    ("cooldown removed", ["cooldown removed", "no cooldown after rebalanceSwap"]), ("MAX_SWAPS removed", ["MAX_SWAPS removed"]),
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
    ("baseOwed minted twice or never", ["baseOwed minted twice", "deferral dropped", "baseOwed never minted", "second deferral overwrites baseOwed"]),
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
    if m[0] in ("relational", "deletion", "operand", "literal"):
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
            if "TierInvalid" in r.stderr:
                return name, "killed", "tier validation (deploy-script configuration check)"
            crashes = 1
        if named:
            return name, "killed", f"self-check: {named} named failures"
        vd = os.path.join(d, "vec")
        env = dict(os.environ, VECTORS_OUT=vd)
        rv = subprocess.run([sys.executable, "-c", "import vectors; vectors.main()"], cwd=d, env=env,
                            capture_output=True, text=True)
        if rv.returncode != 0:
            tb = [l for l in rv.stderr.splitlines() if l.strip().startswith("File ")]
            if "AssertionError" in rv.stderr and tb and "vectors.py" in tb[-1]:
                return name, "killed", "vector generator assertion"
            return name, "INVALID", "vector generator crashed: " + rv.stderr.strip().splitlines()[-1][:120]
        for f in sorted(x for x in os.listdir(VEC) if x.endswith(".json")):  # every committed vector file (s2: clock, mode, scenario)
            if not filecmp.cmp(os.path.join(vd, f), os.path.join(VEC, f), shallow=False):
                return name, "killed", f"vectors differ ({f})"
        if crashes:
            return name, "INVALID", "crash only"
        return name, "SURVIVED", fuzz_trace_of(d)
    finally:
        shutil.rmtree(d, ignore_errors=True)


def main():
    jobs = int(sys.argv[sys.argv.index("-j") + 1]) if "-j" in sys.argv else 8
    src = open(os.path.join(HERE, "reference.py")).read()
    base = subprocess.run([sys.executable, "-c", "import selfcheck,sys; sys.exit(selfcheck.main())"], cwd=HERE,
                          capture_output=True, text=True)
    if base.returncode != 0:
        print("the clean self-check fails; fix it before measuring mutants:\n" + base.stdout[-800:])
        return 2
    allm = MUTANTS + relational_mutants(src) + deletion_mutants(src) + operand_mutants(src) + literal_mutants(src)
    clean = fuzz_trace_of(HERE)
    names = [m[1] for m in allm]
    assert len(names) == len(set(names)), "duplicate mutant names"
    with ThreadPoolExecutor(jobs) as ex:
        results = list(ex.map(lambda m: run_one(src, m), allm))
    status = {}
    claimed = [m for m, (_, st, _) in zip(allm, results) if st == "SURVIVED" and (m[1] in EQUIVALENT or m[1] in UNREACHABLE)]
    with ThreadPoolExecutor(jobs) as ex:
        hits = dict(zip([m[1] for m in claimed], ex.map(lambda m: probe_hits(src, m), claimed)))
    for m, (_, st, why) in zip(allm, results):
        rule, name = m[0], m[1]
        if st == "SURVIVED" and name in hits:
            ran, cond, cond_ext = hits[name]
            same = why == clean
            if name in EQUIVALENT and same and cond_ext > 0:
                st, why = "equivalent", f"{EQUIVALENT[name]} [condition reached {cond}x, identical trace]"
            elif name in UNREACHABLE and same and ran > 0 and cond == 0:
                st, why = "equivalent", f"unreachable: {UNREACHABLE[name]} [site ran {ran}x, condition never; identical trace]"
            else:
                st, why = "CLAIM-REFUTED", (f"trace {'identical' if same else 'DIFFERS'}, site ran {ran}x, condition {cond}x "
                                            f"({cond_ext}x in the fuzz and vector phases)")
        status[name] = (rule, st)
        if st != "killed" or "-v" in sys.argv:
            print(f"{st:15s} [{rule}] {name}" + (f"  ({why})" if why else ""))
    stale = sorted((set(EQUIVALENT) | set(UNREACHABLE)) - set(status))
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
          f"({len(MUTANTS)} named + {len(allm) - len(MUTANTS)} generated: relational, deletion, operand, literal); rules without a killed mutant: {uncovered or 'none'}; "
          f"curated clauses: {len(CLAUSES)} ({n_map} mapped to mutants, {len(CLAUSES) - n_map} with a recorded reason); "
          f"clause problems: {clause_bad or 'none'}")
    return 1 if bad or uncovered or clause_bad else 0


if __name__ == "__main__":
    sys.exit(main())
