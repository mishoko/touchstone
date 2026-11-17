#!/usr/bin/env python3
"""Non-vacuity of the random invariant campaign. `test/invariant/VaultP2.t.sol` appends one line per run to the file
named by TS_NV_LOG (pool, rebalance calls, curator swaps, place calls, placements, moved, pre-swapped). A run may
legitimately see every curator action revert, so the check is on the campaign's totals per pool: enough actions get
past their guards for the invariants to be checked on executed swaps, placements, moves and pre-swaps.

  TS_NV_LOG=output/nonvacuity/inv.csv forge test --mp test/invariant/VaultP2.t.sol
  python3 script/nonvacuity.py output/nonvacuity/inv.csv

The floors are about a third of the rates measured on the 256 x 128 campaign (fixed seed) when they were set: swaps
7.8% of rebalance calls, placements 4.7% of place calls, 79 / 120 moved and 205 / 115 pre-swapped placements."""
import csv, sys

FLOORS = {'swap_rate': 0.025, 'place_rate': 0.015, 'moved': 25, 'pre': 35}


def check(rows):
    agg = {}
    for r in rows:
        a = agg.setdefault(r[0], [0] * 7)
        a[0] += 1
        for i, v in enumerate(r[1:7]):
            a[i + 1] += int(v)
    errs = []
    if set(agg) != {'tick0', 'tick196k'}:
        errs.append(f'expected both pools, got {sorted(agg)}')
    for pool, (runs, rb, sw, pl, pd, mv, pre) in sorted(agg.items()):
        print(f'{pool}: {runs} runs; swaps {sw}/{rb}; placements {pd}/{pl}; moved {mv}; pre-swapped {pre}')
        if rb == 0 or sw / rb < FLOORS['swap_rate']:
            errs.append(f'{pool}: swap rate {sw}/{rb} below {FLOORS["swap_rate"]}')
        if pl == 0 or pd / pl < FLOORS['place_rate']:
            errs.append(f'{pool}: placement rate {pd}/{pl} below {FLOORS["place_rate"]}')
        if mv < FLOORS['moved']:
            errs.append(f'{pool}: {mv} moved placements < {FLOORS["moved"]}')
        if pre < FLOORS['pre']:
            errs.append(f'{pool}: {pre} pre-swapped placements < {FLOORS["pre"]}')
    return errs


def main(argv):
    if argv[1:2] == ['--selftest']:
        good = [['tick0', '100', '8', '100', '5', '30', '40'], ['tick196k', '100', '8', '100', '5', '30', '40']]
        bad = [['tick0', '100', '0', '100', '5', '30', '40'], ['tick196k', '100', '8', '100', '5', '30', '40']]
        ok = not check(good) and len(check(bad)) == 1 and check(good[:1])
        print('nonvacuity selftest', 'OK' if ok else 'FAILED')
        return 0 if ok else 1
    if len(argv) != 2:
        print(__doc__)
        return 2
    errs = check(list(csv.reader(open(argv[1]))))
    for e in errs:
        print('NON-VACUITY:', e)
    print('nonvacuity', 'OK' if not errs else 'FAILED')
    return 1 if errs else 0


if __name__ == '__main__':
    sys.exit(main(sys.argv))
