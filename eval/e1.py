#!/usr/bin/env python3
"""E1 (spec §10): the witness is a real position. Pass: 0 wei (measured).

Assembled from this session's `forge test` (P-18): `test_F_4__witnessFeeLegEqualsA1e18ReferencePosition` fuzzes swap
sequences and asserts with exact equality (assertEq, no tolerance) that the hook's witness fee leg equals the fees of an
independent 1e18-liquidity reference position on the same range, so every passing run is a difference of 0 wei.

  python3 eval/e1.py [--forge-log eval/supplements/forge-s27-tests.log] [--out eval/results/E1.json]
"""
import hashlib, json, os, re, subprocess, sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
TEST = 'test_F_4__witnessFeeLegEqualsA1e18ReferencePosition'


def git(*a, cwd=ROOT):
    return subprocess.run(['git', *a], cwd=cwd, capture_output=True, text=True).stdout.strip()


def main(argv):
    log = argv[argv.index('--forge-log') + 1] if '--forge-log' in argv else 'eval/supplements/forge-s27-tests.log'
    out = argv[argv.index('--out') + 1] if '--out' in argv else None
    txt = open(os.path.join(ROOT, log)).read()
    m = re.search(r'\[PASS\] ' + TEST + r'\([^)]*\) \(runs: (\d+)', txt)
    others = re.findall(r'\[PASS\] (test_F_4__\w+)', txt)
    ok = bool(m)
    res = {
        'id': 'E1', 'question': 'witness = a real position', 'pass_rule': '0 wei (measured)',
        'verdict': 'PASS' if ok else 'FAIL',
        'rule_applied': 'exact equality (assertEq) of the witness fee leg and an independent reference position, on every '
                        'fuzz run',
        'data': 'foundry (fuzzed swap sequences on a local pool)',
        'numbers': {'max_difference_wei': 0 if ok else None, 'fuzz_runs': int(m.group(1)) if m else 0, 'fuzz_seed': '0x5309',
                    'related_tests_passed': others},
        'forge_log': {'path': log, 'sha256': hashlib.sha256(open(os.path.join(ROOT, log), 'rb').read()).hexdigest()},
        'limits': 'synthetic swap sequences, not replay swaps; the fixed seed makes the run a regression check (fresh seeds '
                  'go through script/explore.sh)',
        'commit_public': git('rev-parse', 'HEAD'),
        'commit_internal': git('rev-parse', 'HEAD', cwd=os.path.join(ROOT, 'internal')),
    }
    t = json.dumps(res, indent=1, allow_nan=False) + '\n'
    if out:
        open(os.path.join(ROOT, out), 'w').write(t)
    print(t)
    return 0 if ok else 1


if __name__ == '__main__':
    sys.exit(main(sys.argv[1:]))
