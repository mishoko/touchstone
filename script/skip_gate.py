#!/usr/bin/env python3
"""Fails a `forge test` log that skipped any test once the claims phase is P5 or later (P5 code panel P5-Ad-03).
The fork rows (K-1, K-ref, G-1b) skip themselves without a chain-1 RPC, and claims.py matches test names only, so a
skipped fork row would otherwise still count as covered.

  python3 script/skip_gate.py <forge-test-log> [PHASE]   # PHASE defaults to $CLAIMS_PHASE, then P0
  python3 script/skip_gate.py --selftest"""
import os, re, sys

SUMMARY = re.compile(r'Ran \d+ test suites? in .*?: (\d+) tests? passed, (\d+) failed, (\d+) skipped')


def phase_at_least(phase, n):
    m = re.fullmatch(r'P(\d+)\w*', phase or '')
    return bool(m) and int(m.group(1)) >= n


def check(text, phase):
    """(ok, message)."""
    if not phase_at_least(phase, 5):
        return True, f'skip gate: not enforced at {phase}'
    rows = SUMMARY.findall(text)
    if not rows:
        return False, 'skip gate: no forge test summary line in the log'
    passed, failed, skipped = map(int, rows[-1])
    if skipped:
        return False, f'skip gate: {skipped} test(s) skipped at {phase} (a fork row without its RPC?); fail'
    return True, f'skip gate: {passed} passed, {failed} failed, 0 skipped at {phase}'


def selftest():
    ok_log = 'Ran 60 test suites in 9.1s (80s CPU time): 455 tests passed, 0 failed, 0 skipped (455 total tests)'
    skip_log = 'Ran 60 test suites in 9.1s (80s CPU time): 452 tests passed, 0 failed, 3 skipped (455 total tests)'
    assert check(ok_log, 'P5')[0] and check(ok_log, 'P6')[0]
    assert not check(skip_log, 'P5')[0] and not check(skip_log, 'P7b')[0]
    assert check(skip_log, 'P4')[0] and check(skip_log, 'P0')[0]
    assert not check('no summary', 'P6')[0]
    print('skip gate selftest OK')


if __name__ == '__main__':
    if sys.argv[1:] == ['--selftest']:
        selftest()
        sys.exit(0)
    ok, msg = check(open(sys.argv[1]).read(), sys.argv[2] if len(sys.argv) > 2 else os.environ.get('CLAIMS_PHASE', 'P0'))
    print(msg)
    sys.exit(0 if ok else 1)
