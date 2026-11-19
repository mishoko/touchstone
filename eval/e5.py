#!/usr/bin/env python3
"""E5 (spec §10): every attack cell. Pass: <= 0, with a positive control > 0.

Built from one `forge test -vv` log of the shipped contracts (this session's, fork pinned). A cell is a threat ID
(the `test_M_<cell>__` prefix). Pre-registered in the s27 P6 plan (design review S27P6D-06), before the log existed:
- each cell's attack tests assert that the attack does not profit (net <= 0); they must all pass;
- each cell has a positive control: a control test (`ctl`/`control` in its name, asserting that the attack profits
  with the defence removed) that passes, or, for the three cells whose controls are assertions inside the attack test,
  that inline assertion (named below with its line); a cell without either is reported "no positive control" and
  E5 is FAIL;
- the logged numbers (worst net, control net) are copied from the log as printed; a cell whose test logs no number is
  listed with its assertion only.
Python cells (lag leak L1/L2, grief L4, ADV-3 under a phi-scaled charge) are cited from earlier sessions with their
configuration and are not gated here.

  python3 eval/e5.py [--forge-log output/s27-forge-test.log] [--out eval/results/E5.json]
"""
import hashlib, json, os, re, subprocess, sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
INLINE = {
    'M_N1': 'test/manipulation/MN1.t.sol:135 (parking earns tau_v and base at the tier m: credit > 0)',
    'M_N2': 'test/manipulation/MN2.t.sol:122 and :184 (one more in-range step moves the base amount; the timing gain is '
            'visible to the measure)',
    'M_N3': 'test/manipulation/MN3.t.sol:99 (a forgery cell: the frozen-median comparison discriminates)',
}
PY_CELLS = {
    'lag_leak_L1_L2': '0.000% / 0.000% NAV/yr at sigma 60 / 120% (bounds 0.1 / 0.6), held-out seeds 300-499, s17 '
                      'configuration; status-quo control 8.31% / 37.2%',
    'grief_L4': 'griefer net > 0 on 0 rows (s5/s17); control without re-alignment profits',
    'adv3_phi_scaled': 'ADV-3 under a phi = 10% charge earns about 10x base (control); under the 100% charge max net '
                       '-2.9e9 wei',
}


def sha(p):
    return hashlib.sha256(open(os.path.join(ROOT, p), 'rb').read()).hexdigest()


def git(*a, cwd=ROOT):
    return subprocess.run(['git', *a], cwd=cwd, capture_output=True, text=True).stdout.strip()


def parse(txt):
    """{test name: (status, [log lines])} for every test_M_ test in a forge -vv log."""
    out, cur = {}, None
    for ln in txt.split('\n'):
        m = re.match(r'\[(PASS|FAIL)[^\]]*\] (test_M_\w+)\(', ln)
        if m:
            cur = m.group(2)
            out[cur] = (m.group(1), [])
            continue
        if re.match(r'\[(PASS|FAIL)', ln) or ln.startswith('Suite result') or ln.startswith('Ran '):
            cur = None
            continue
        if cur and ln.startswith('  ') and ln.strip() and ln.strip() != 'Logs:':
            out[cur][1].append(ln.strip())
    return out


def main(argv):
    log = argv[argv.index('--forge-log') + 1] if '--forge-log' in argv else 'output/s27-forge-test.log'
    out = argv[argv.index('--out') + 1] if '--out' in argv else None
    tests = parse(open(os.path.join(ROOT, log)).read())
    cells, ok = {}, bool(tests)
    for name, (st, lines) in sorted(tests.items()):
        cell, _, rest = name[len('test_'):].partition('__')
        c = cells.setdefault(cell, dict(tests=0, passed=0, controls=[], logs={}))
        c['tests'] += 1
        c['passed'] += st == 'PASS'
        if re.search(r'ctl|control', rest, re.I):
            c['controls'].append(dict(test=name, passed=st == 'PASS'))
        if lines:
            c['logs'][rest] = lines[:12]
    for cell, c in cells.items():
        has_ctl = any(x['passed'] for x in c['controls'])
        c['positive_control'] = 'control test' if has_ctl else (('inline: ' + INLINE[cell]) if cell in INLINE
                                                                 else 'no positive control')
        c['ok'] = c['passed'] == c['tests'] and c['positive_control'] != 'no positive control'
        ok &= c['ok']
    res = {
        'id': 'E5', 'question': 'every attack cell', 'pass_rule': '<= 0, with a positive control > 0',
        'verdict': 'PASS' if ok else 'FAIL',
        'rule_applied': 'every attack test of every cell passes (each asserts the attack does not profit) and every cell '
                        'has a passing positive control (a control test, or the named inline assertion)',
        'data': 'foundry (the shipped contracts; mainnet fork for the K-ref costs, block 26443420)',
        'cells': cells, 'python_cells_reported': PY_CELLS,
        'forge_log': {'path': log, 'sha256': sha(log)},
        'limits': 'the cells are scripted attacks (with searched parameters) on a local pool, not an exhaustive search; '
                  'the Python cells come from earlier configurations and are reported, not gated; MK-1 (a single '
                  'builder holding the reference over the markout window) is outside the model',
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
