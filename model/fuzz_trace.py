#!/usr/bin/env python3
"""Randomized differential trace of the reference model.

Runs seeded random sequences (TEST and DEMO tiers, start blocks from 3 to 12,000, timestamp phases) of every entry point (placements at random legal and edge ranges, deposits,
withdrawals, swaps, pre-swaps, pokes, retire, claim, top-up, release, reference moves including far moves and
unreadable windows, pool pushes, silent gaps, unplaced vaults) and prints one line per step with the outcome and
the full model state. `mutants_model.py` uses it as the witness for every equivalence claim: an equivalent mutant
must reproduce the clean trace exactly.
"""
import hashlib
import json
import random

import reference as R
import selfcheck as C
from v4math import get_sqrt_price_at_tick as sqrt_at

T0 = C.T0


def state(w):
    """The complete model state, canonicalised (vault, hook, bank, records, pool)."""
    import probe_rt
    return probe_rt.canon(w.v)


def run(seed, steps=80):
    rng = random.Random(seed)
    st = {"ref": T0, "fail": False}
    third = rng.choice([0, 10 ** 15, 10 ** 19, 10 ** 22])
    seed_amt = rng.choice([(150_000 * C.USDC, 50 * C.ETH), (300_000 * C.USDC, 1 * C.ETH), (1_000 * C.USDC, 90 * C.ETH)])
    tier = rng.choice([R.TEST, R.TEST, R.DEMO])
    start = rng.choice([3, 40, 400, 12000])
    phase = 10 ** 6 + rng.choice([0, 150]) if start < 1000 else rng.choice([0, 150])
    w = C.World(tier=tier, ref_fn=lambda ts: st["ref"], third_L=third, seed_amounts=seed_amt, phase=phase,
                start_block=start)
    w.ref.fail = lambda end: st["fail"]
    out = []
    for i in range(steps):
        op = rng.choice(["place"] * 4 + ["swap"] * 4 + ["deposit", "withdraw", "poke", "poke", "step", "touch", "touch",
                         "ref", "push", "claim", "topup", "retire", "release", "gap", "same", "breach"] + (["far"] if rng.random() < 0.15 else []))
        res = "ok"
        try:
            if op == "place":
                ref, spot = st["ref"], w.v.pool.tick
                kind = rng.choice(["edge0", "edge1", "rand"])
                if kind == "edge0":
                    lo = min(ref, spot) // 10 * 10
                elif kind == "edge1":
                    lo = max(ref, spot) // 10 * 10 + 10 - 2000
                else:
                    a_, b_ = max(ref, spot) // 10 * 10 + 10 - 2000, min(ref, spot) // 10 * 10
                    lo = rng.randrange(a_, b_ + 1, 10) if a_ <= b_ else a_  # an illegal range when the fence fails
                pre = None
                if rng.random() < 0.3:
                    z = rng.random() < 0.5
                    pre = dict(active=True, zeroForOne=z, amountIn=max(1, (w.v.h.idle0 if z else w.v.h.idle1) // rng.choice([5, 50, 500])))
                if rng.random() < 0.4:
                    w.v.pool.push_to(sqrt_at(lo) + 1 if kind == "edge0" else sqrt_at(lo + 2000) - 1)
                w.place(lo, lo + 2000, pre)
            elif op == "deposit":
                w.deposit(rng.choice([10 ** 18, 10 ** 24, 10 ** 26 + 7]))
            elif op == "withdraw":
                if w.depositor:
                    w.withdraw(rng.randint(1, w.depositor))
            elif op == "swap":
                zfo = rng.random() < 0.5
                idle = w.v.h.idle0 if zfo else w.v.h.idle1
                w.v.rebalance_swap(zfo, max(1, int(idle * rng.choice([0.001, 0.01, 0.1, 1.0]))), w.block, w.ts())
            elif op == "poke":
                w.poke()
            elif op == "step":
                w.step(rng.randint(1, 60))
            elif op == "touch":
                w.step(rng.randint(1, 25))
                w.touch()
            elif op == "same":
                w.touch_same_block()
            elif op == "breach":
                w.v.process_breach(w.block, w.ts())
            elif op == "ref":
                st["ref"] += rng.randint(-80, 80)
                st["fail"] = rng.random() < 0.1
            elif op == "push":
                w.v.pool.push_to(sqrt_at(st["ref"] + rng.randint(-60, 60)))
            elif op == "claim":
                w.claim()
            elif op == "topup":
                w.v.top_up_bond(rng.randint(1, 10 ** 20), w.block, w.ts())
            elif op == "retire":
                if rng.random() < 0.2:
                    w.v.retire(w.block, w.ts())
            elif op == "release":
                w.release()
            elif op == "gap":
                w.step(rng.randint(2, 6) * w.t.GRID * 2)
            elif op == "far":
                st["ref"] = T0 + rng.choice([-300000, 1500, -1500, 0])
        except (R.Revert, R.MathRevert) as ex:
            res = getattr(ex, "err", str(ex))
        out.append(json.dumps([i, op, res, state(w)], sort_keys=True, default=str))
    return out


def main(seeds=range(48)):
    h = hashlib.sha256()
    for s in seeds:
        for line in run(s):
            h.update(line.encode())
    print("TRACE", h.hexdigest())


if __name__ == "__main__":
    main()
