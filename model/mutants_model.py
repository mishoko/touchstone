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
MUTANTS = [
    ("youngest-first burn -> oldest-first", "for c in sorted(b.cohorts, key=lambda c: -c[0]):",
     "for c in sorted(b.cohorts, key=lambda c: c[0]):"),
    ("live leg ignored", "gate_open = ok and in_rng(lo, hi, t)", "gate_open = True"),
    ("live read when not vestable", "        if gate and vestable:", "        if gate:"),
    ("0 floor removed", "if loss > 0 else 0", "if loss > 0 else loss"),
    ("reference leg dropped", "    for p in (sqrt_spot, sqrt_ref):", "    for p in (sqrt_spot,):"),
    ("RESET_RUN = 1", "if self.resetStreak >= self.t.RESET_RUN:", "if self.resetStreak >= 1:"),
    ("gap flag never set", "        if now > self.refCachedAt + 2 * G:", "        if False:"),
    ("removal not reached", "            return EXITED, b, False", "            return BREACHED, b, False"),
    ("release delay from dripBlocks", "if now < b.exitBlock + b.releaseDelay:", "if now < b.exitBlock + b.dripBlocks:"),
    ("mode gate skipped", "        if not ladder_allowed(self.bank.mode, E[entry]):", "        if False:"),
    ("bond burn reported as bank", "return burn, 0, bank_burn, need", "return burn, 0, bank_burn + need, 0"),
    ("no staleness truncation", "        trunc = max(0, min(now, self.refCachedAt + 2 * G) - last)",
     "        trunc = max(0, now - last)"),
    ("headroom removed", "head = L0 // 10 ** 9 + 1", "head = 0"),
    ("latch block wrong", "self.lastPendingBlock = self.lastSnap + (j * C - old)", "self.lastPendingBlock = self.lastSnap"),
    ("vest one epoch early", "if c[1] > 0 and ne >= c[0] + 1 + V:", "if c[1] > 0 and ne >= c[0] + V:"),
    ("drip burns all at once", "    if b.dripBlocks == 0:", "    if True:"),
    ("ring overwrite merges occupant", "        if slot[1] > 0 and slot[0] != e:", "        if False:"),
    ("τ_v at the edge", "get_sqrt_price_at_tick(lo) < sqrt_price < get_sqrt_price_at_tick(hi)",
     "get_sqrt_price_at_tick(lo) <= sqrt_price < get_sqrt_price_at_tick(hi)"),
    ("EMPTY not counted", "        if not in_rng(lo, hi, self.refTick):",
     "        if not in_rng(lo, hi, self.refTick) and not self.emptyRange:"),
    ("Place from EXITED allowed", "        if mode == EXITED:\n            raise Revert(\"BadMode\")\n        return NORMAL",
     "        return NORMAL"),
    ("exitBlock rewritten after exit", "            return EXITED, b, False\n        if b >= 3:",
     "            return EXITED, b, True\n        if b >= 3:"),
]


def main():
    src = open(os.path.join(HERE, "reference.py")).read()
    survivors = []
    for name, old, new in MUTANTS:
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
        print(("killed   " if killed else "SURVIVED ") + name)
        if not killed:
            survivors.append(name)
    print(f"{len(MUTANTS) - len(survivors)}/{len(MUTANTS)} killed")
    return 1 if survivors else 0


if __name__ == "__main__":
    sys.exit(main())
