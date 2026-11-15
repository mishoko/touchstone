#!/usr/bin/env python3
"""Standing check for the reference model: each mutant below breaks one rule of the specification in
`reference.py`; the self-check must fail on every one (gate: 100% killed). Run from the repository root:
    python3 model/mutants_model.py
"""
import os
import shutil
import subprocess
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
RULES = {
    "ledger": "worse-of charge over {spot, reference}, 0 floor, p*, one-price burn and gate",
    "fence": "|spot − reference| <= REF_BAND on every curator action",
    "base": "base fee: δ-weighted, observed in-range blocks, eligibility (placed, NORMAL/BREACHED), deferral",
    "bank": "cohorts, youngest-first burn, bond, vesting after V epochs, drip, release delay",
    "gate": "vest gate: fresh cache in range, no gap, live leg read once only when vestable, maturedPending",
    "snapshot": "snapshot steps 1-6: staleness truncation, gap flag, reset streak on consecutive anchors, one refresh per anchor",
    "clock": "breach clock and latch per CURE_BLOCKS (block and τ_v at the latch), EMPTY counted",
    "pending": "pending breaches processed first: forfeit unvested and baseOwed, ack",
    "registration": "registration writes the cache, clears the gap, starts a run",
    "modes": "mode machine incl. the gate after the prologue and exitBlock on transition only",
    "placement": "Placement.check/size: guards, headroom, δ_after, saturating legs",
    "caps": "swap caps per action, per epoch, MAX_SWAPS, idle, counters reset at the roll",
    "epilogue": "epilogue re-bases (actions at the live reference, flows NAV0·S'/S), collect-again",
    "rounding": "rounding directions and the NAV cap in shares",
    "supply": "share supply conservation",
}
MUTANTS = [
    ("bank", "youngest-first burn -> oldest-first", "for c in sorted(b.cohorts, key=lambda c: -c[0]):",
     "for c in sorted(b.cohorts, key=lambda c: c[0]):"),
    ("gate", "live leg ignored", "gate_open = ok and in_rng(lo, hi, t)", "gate_open = True"),
    ("gate", "live read when not vestable", "        if gate and vestable:", "        if gate:"),
    ("gate", "cached-range leg dropped", ' and not o["gapSinceVerify"] and in_rng(lo, hi, o["refTick"])',
     ' and not o["gapSinceVerify"]'),
    ("ledger", "0 floor removed", "if loss > 0 else 0", "if loss > 0 else loss"),
    ("ledger", "reference leg dropped", "    for p in (sqrt_spot, sqrt_ref):", "    for p in (sqrt_spot,):"),
    ("rounding", "burn rounds down", "burn = ceil_div(charge * S, nav_after)", "burn = charge * S // nav_after"),
    ("snapshot", "RESET_RUN = 1", "if self.resetStreak >= self.t.RESET_RUN:", "if self.resetStreak >= 1:"),
    ("snapshot", "gap flag never set", "        if now > self.refCachedAt + 2 * G:", "        if False:"),
    ("snapshot", "streak ignores consecutive anchors", "if self.refGrid + 1 == k else 1", "if True else 1"),
    ("snapshot", "out-of-range sample keeps the streak",
     "        else:\n            self.resetStreak = 0\n        self._write_cache(t, k, now)",
     "        else:\n            pass\n        self._write_cache(t, k, now)"),
    ("snapshot", "refresh twice per anchor (snapshot path)", "if k > self.refGrid else None", "if k >= self.refGrid else None"),
    ("snapshot", "refresh twice per anchor (_refresh)", "        if k > self.refGrid:\n            ok, t",
     "        if k >= self.refGrid:\n            ok, t"),
    ("snapshot", "no staleness truncation", "        trunc = max(0, min(now, self.refCachedAt + 2 * G) - last)",
     "        trunc = max(0, now - last)"),
    ("clock", "outBlocks untruncated", "self.outBlocks = sat(self.outBlocks + trunc, U64)",
     "self.outBlocks = sat(self.outBlocks + (now - last), U64)"),
    ("modes", "removal not reached", "            return EXITED, b, False\n        if b >= 3:",
     "            return BREACHED, b, False\n        if b >= 3:"),
    ("bank", "release delay from dripBlocks", "if now < b.exitBlock + b.releaseDelay:", "if now < b.exitBlock + b.dripBlocks:"),
    ("modes", "mode gate skipped", "        if not ladder_allowed(self.bank.mode, E[entry]):", "        if False:"),
    ("ledger", "bond burn reported as bank", "return burn, 0, bank_burn, need", "return burn, 0, bank_burn + need, 0"),
    ("placement", "headroom removed", "head = L0 // 10 ** 9 + 1", "head = 0"),
    ("clock", "latch block wrong", "self.lastPendingBlock = self.lastSnap + (j * C - old)", "self.lastPendingBlock = self.lastSnap"),
    ("bank", "vest one epoch early", "if c[1] > 0 and ne >= c[0] + 1 + V:", "if c[1] > 0 and ne >= c[0] + V:"),
    ("bank", "drip burns all at once", "    if b.dripBlocks == 0:", "    if True:"),
    ("bank", "ring overwrite merges occupant", "        if slot[1] > 0 and slot[0] != e:", "        if False:"),
    ("base", "τ_v at the edge", "get_sqrt_price_at_tick(lo) < sqrt_price < get_sqrt_price_at_tick(hi)",
     "get_sqrt_price_at_tick(lo) <= sqrt_price < get_sqrt_price_at_tick(hi)"),
    ("clock", "EMPTY not counted", "        if not in_rng(lo, hi, self.refTick):",
     "        if not in_rng(lo, hi, self.refTick) and not self.emptyRange:"),
    ("modes", "Place from EXITED allowed", "        if mode == EXITED:\n            raise Revert(\"BadMode\")\n        return NORMAL",
     "        return NORMAL"),
    ("modes", "exitBlock rewritten after exit", "            return EXITED, b, False\n        if b >= 3:",
     "            return EXITED, b, True\n        if b >= 3:"),
    ("base", "deferral dropped", "                self.baseOwed = amount\n", "                pass\n"),
    ("base", "always mint at tryRef", '            if o["refFresh"]:\n                price = o["refTick"]',
     '            if False:\n                price = o["refTick"]'),
    ("base", "base accrues in EXITED", "return self.bank.placedByCurator and self.bank.mode in (NORMAL, BREACHED)", "return True"),
    ("pending", "baseOwed kept at a breach", '            r["forfeitValue"] += self.baseOwed\n            self.baseOwed = 0',
     '            r["forfeitValue"] += self.baseOwed'),
    ("pending", "baseOwed kept at retire", "            fv = self.baseOwed\n            self.baseOwed = 0", "            fv = self.baseOwed"),
    ("pending", "ackBreaches skipped", "            self.hook.ack_breaches(n)", "            pass"),
    ("supply", "charge burn not removed from supply", "        self.S -= bb\n", "\n"),
    ("registration", "registration keeps the gap",
     "        self._write_cache(live_tick, ts // self.t.GRID_SEC, now)\n        self.gapSinceVerify = False",
     "        self._write_cache(live_tick, ts // self.t.GRID_SEC, now)"),
    ("registration", "registration does not write the cache", "        self._write_cache(live_tick, ts // self.t.GRID_SEC, now)\n", ""),
    ("fence", "rebalanceSwap fence removed", '            raise Revert("Fence")', "            pass"),
    ("caps", "MAX_SWAPS removed", "if self.swapCount >= t.MAX_SWAPS:", "if False:"),
    ("caps", "per-action cap removed", "if frac > t.MAX_SWAP_ACTION or self.epochSwapFrac", "if False or self.epochSwapFrac"),
    ("caps", "epoch cap removed", "or self.epochSwapFrac + frac > t.MAX_SWAP_EPOCH:", "or False:"),
    ("caps", "counters not reset at the roll", "            self.epochSwapFrac = 0\n            self.swapCount = 0",
     "            self.epochSwapFrac = 0"),
    ("caps", "idle check removed", "        if amount_in > (self.h.idle0 if zfo else self.h.idle1):", "        if False:"),
    ("epilogue", "no collect after the vault's own swap", "        self.swapCount += 1\n        self._collect_first()",
     "        self.swapCount += 1"),
    ("epilogue", "rebalanceSwap epilogue missing",
     "            self.lastAction = now\n            self.NAV0, self.delta0 = self.nav(s_ref), delta_of(self.h, s_ref)\n            return charge, p_star, burn\n",
     "            self.lastAction = now\n            return charge, p_star, burn\n"),
    ("epilogue", "withdraw re-base missing", "            self.h.idle1 -= o1\n            self._rebase_flow(S)", "            self.h.idle1 -= o1"),
    ("rounding", "NAV cap removed", "if self.S * self.t.BOND_RATIO > self.bank.bondShares * WAD:", "if False:"),
    ("rounding", "deposit rounds down", "ceil_div(self.h.idle0 * shares, S), ceil_div(self.h.idle1 * shares, S)",
     "self.h.idle0 * shares // S, self.h.idle1 * shares // S"),
    ("placement", "saturating leg reverts", "    return min(amount0 * inter // (sb - sa), U128)",
     "    r = amount0 * inter // (sb - sa)\n    assert r <= U128\n    return r"),
]


def main():
    src = open(os.path.join(HERE, "reference.py")).read()
    survivors = []
    killed_rules = set()
    for rule, name, old, new in MUTANTS:
        if src.count(old) != 1:
            print(f"NOT APPLICABLE (pattern count {src.count(old)}): {name}")
            survivors.append(name)
            continue
        d = tempfile.mkdtemp()
        for f in os.listdir(HERE):
            if f.endswith(".py"):
                shutil.copy(os.path.join(HERE, f), d)
        open(os.path.join(d, "reference.py"), "w").write(src.replace(old, new))
        r = subprocess.run([sys.executable, "reference.py", "--selfcheck"], cwd=d, capture_output=True, text=True)
        shutil.rmtree(d)
        killed = r.returncode != 0
        print(("killed   " if killed else "SURVIVED ") + f"[{rule}] {name}")
        if killed:
            killed_rules.add(rule)
        else:
            survivors.append(name)
    uncovered = sorted(set(RULES) - killed_rules)
    print(f"{len(MUTANTS) - len(survivors)}/{len(MUTANTS)} killed; rules without a killed mutant: {uncovered or 'none'}")
    return 1 if survivors or uncovered else 0


if __name__ == "__main__":
    sys.exit(main())
