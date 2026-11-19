#!/usr/bin/env python3
"""Non-vacuity of the random invariant campaign. `test/invariant/VaultP2.t.sol` appends one line per run to the file
named by TS_NV_LOG (pool, rebalance calls, curator swaps, place calls, placements, moved, pre-swapped). A run may
legitimately see every curator action revert, so the check is on the campaign's totals per pool (I-15's counters, s20, each > 0): enough actions get
past their guards for the invariants to be checked on executed swaps, placements, moves and pre-swaps.

  TS_NV_LOG=output/nonvacuity/inv.csv forge test --mp test/invariant/VaultP2.t.sol
  python3 script/nonvacuity.py output/nonvacuity/inv.csv

The floors are about a third of the rates measured on the 256 x 128 campaign (fixed seed) when they were set (s13,
tick 0 / tick 196k): swaps 1,210 / 1,152 of 15,288 rebalance calls (7.9% / 7.5%), placements 708 / 708 of 15,114 place
calls (4.7%), 103 / 126 moved and 204 / 120 pre-swapped placements."""
import csv, sys

FLOORS = {'swap_rate': 0.025, 'place_rate': 0.015, 'moved': 25, 'pre': 35, 'base': 1}
# the P4 handler's counters (columns 8..17 of a row): campaign totals per pool, each at least this (F-048 (d); the
# floors are set from the s19 P4-close campaign, about a third of its counts and never below 1)
P4 = ['claims', 'topUps', 'retires', 'releases', 'pokes', 'endgames', 'i3DrainChecks', 'i12Moved', 'forfeitedReserveSettles',
      'paidPayouts']
# measured at the s19 P4-close campaign (256 x 128, fixed seed; tick0 / tick196k): claims 16,560 / 16,560, topUps 588 /
# 528, retires 836 / 850, releases 8,415 / 8,461, pokes 16,730 / 16,730, endgames 10,326 / 10,232, i3DrainChecks
# 102,615 / 102,552, i12Moved 428 / 393, forfeitedReserveSettles 844 / 894
P4_FLOORS = {'claims': 5000, 'topUps': 170, 'retires': 270, 'releases': 2800, 'pokes': 5500, 'endgames': 3400,
             'i3DrainChecks': 34000, 'i12Moved': 130, 'forfeitedReserveSettles': 280,
             # paidPayouts (S19C-03) added after the campaign: the structural minimum until the next campaign measures it
             'paidPayouts': 1}
# I-15 (spec §9.1, s20): the P5 counters (columns 18..32), each > 0 over the campaign per pool, as the spec's row states
P5 = ['i8Checks', 'chargedRecords', 'breachesProcessed', 'removals', 'vests', 'epochCloses', 'refreshes', 'refreshFails',
      'truncatedSpans', 'gapSets', 'gapClears', 'gateClosedLive', 'resetsByRefresh', 'ringWraps', 'i13Checks']
# P5-Ad-05 (P5 code panel, s27): vests in runs that planted no pay through the harness seam, so the real chain base mint
# -> cohort -> vest ran (column 33); `mintAndVestCalls` (column 34, s28): the seam-free sequence's completed runs.
# Floors (s28 fix review S28M-02): about 20% of the first measured 256 x 128 campaign with the sequence (realMintVests
# 4,985 / 5,468; mintAndVestCalls 3,136 / 3,278 at the s28 re-run with
# the counters separated: realMintVests 5,423 / 5,404).
REAL = ['realMintVests', 'mintAndVestCalls']
REAL_FLOORS = {'realMintVests': 1000, 'mintAndVestCalls': 600}
NCOL = 7 + len(P4) + len(P5) + len(REAL)


def check(rows):
    agg = {}
    errs_rows = []
    for r in rows:
        a = agg.setdefault(r[0], [0] * (NCOL + 1))
        a[0] += 1
        if len(r) != NCOL + 1:
            errs_rows.append(f'{r[0]}: a row with {len(r) - 1} counters, expected {NCOL}')
            continue
        for i, v in enumerate(r[1:NCOL + 1]):
            a[i + 1] += int(v)
    errs = errs_rows[:1]
    if set(agg) != {'tick0', 'tick196k'}:
        errs.append(f'expected both pools, got {sorted(agg)}')
    for pool, a in sorted(agg.items()):
        runs, rb, sw, pl, pd, mv, pre, bm = a[:8]
        p4 = dict(zip(P4, a[8:8 + len(P4)]))
        p5 = dict(zip(P5, a[8 + len(P4):8 + len(P4) + len(P5)]))
        real = dict(zip(REAL, a[8 + len(P4) + len(P5):]))
        print(f'{pool}: {runs} runs; swaps {sw}/{rb}; placements {pd}/{pl}; moved {mv}; pre-swapped {pre}; withdrawals minting base {bm}')
        if rb == 0 or sw / rb < FLOORS['swap_rate']:
            errs.append(f'{pool}: swap rate {sw}/{rb} below {FLOORS["swap_rate"]}')
        if pl == 0 or pd / pl < FLOORS['place_rate']:
            errs.append(f'{pool}: placement rate {pd}/{pl} below {FLOORS["place_rate"]}')
        if mv < FLOORS['moved']:
            errs.append(f'{pool}: {mv} moved placements < {FLOORS["moved"]}')
        if pre < FLOORS['pre']:
            errs.append(f'{pool}: {pre} pre-swapped placements < {FLOORS["pre"]}')
        if bm < FLOORS['base']:
            errs.append(f'{pool}: {bm} withdrawals minted base < {FLOORS["base"]} (the mint path is not fuzzed)')
        print(f'{pool}: ' + ', '.join(f'{k} {v}' for k, v in p4.items()))
        for k in P4:
            if p4[k] < P4_FLOORS[k]:
                errs.append(f'{pool}: {k} {p4[k]} < {P4_FLOORS[k]}')
        print(f'{pool}: ' + ', '.join(f'{k} {v}' for k, v in p5.items()))
        for k in P5:
            if p5[k] < 1:
                errs.append(f'{pool}: I-15 counter {k} is 0')
        print(f'{pool}: ' + ', '.join(f'{k} {v}' for k, v in real.items()))
        for k in REAL:
            if real[k] < REAL_FLOORS[k]:
                errs.append(f'{pool}: {k} {real[k]} < {REAL_FLOORS[k]} (the real base-mint vest path is under-fuzzed)')
    return errs


def main(argv):
    if argv[1:2] == ['--selftest']:
        tail = [str(P4_FLOORS[k] * 3) for k in P4] + ['5'] * len(P5) + [str(REAL_FLOORS[k] * 3) for k in REAL]
        good = [['tick0', '100', '8', '100', '5', '30', '40', '3'] + tail, ['tick196k', '100', '8', '100', '5', '30', '40', '3'] + tail]
        bad = [['tick0', '100', '0', '100', '5', '30', '40', '3'] + tail, good[1]]
        ok = not check(good) and len(check(bad)) == 1 and check(good[:1])
        ok = ok and len(check([g[:7] + ['0'] + tail for g in good])) == 2  # a campaign that never mints base fails
        for i in range(len(P4)):  # each P4 counter at zero fails on both pools
            ok = ok and len(check([g[:8 + i] + ['0'] + g[9 + i:] for g in good])) == 2
        for i in range(len(P5)):  # each I-15 counter at zero fails on both pools
            j = 8 + len(P4) + i
            ok = ok and len(check([g[:j] + ['0'] + g[j + 1:] for g in good])) == 2
        j = 8 + len(P4) + len(P5)  # P5-Ad-05: no real-mint vest fails on both pools
        ok = ok and len(check([g[:j] + ['0'] + g[j + 1:] for g in good])) == 2
        ok = ok and len(check([g[:j + 1] + ['0'] for g in good])) == 2  # s28: no completed mintAndVest fails on both
        ok = ok and len(check([g[:j + 1] for g in good])) >= 1  # an s27-format row (no mintAndVestCalls column) fails
        ok = ok and len(check([g[:j] for g in good])) >= 1  # an s26-format row (no real-mint column) fails
        ok = ok and len(check([g[:8] for g in good])) >= 1  # an old-format row (no P4 counters) fails
        ok = ok and len(check([g[:8 + len(P4)] for g in good])) >= 1  # an s19-format row (no P5 counters) fails
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
