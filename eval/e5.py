#!/usr/bin/env python3
"""E5 (spec §10): every attack cell. Pass: <= 0, with a positive control > 0.

Built from one `forge test -vv` log of the shipped contracts (this session's, fork pinned). A cell is a threat ID
(the `test_M_<cell>__` prefix). Pre-registered in the s27 P6 plan (design review S27P6D-06), before the log existed:
- each cell's attack tests assert that the attack does not profit (net <= 0); they must all pass;
- each cell has a positive control: a control test (`ctl`/`control` in its name, asserting that the attack profits
  with the defence removed) that passes; the registered plan exempted N3 only (its discrimination control,
  MN3.t.sol:99), and a cell without a control test is "no positive control", E5 FAIL;
- RULE DEVIATION (recorded after the data, fix review S27P6F-02): N1 and N2 also have no separate control test; their
  positive controls are asserted inside the attack tests (MN1.t.sol:135, MN2.t.sol:122/184). Under the registered rule
  E5 would be FAIL on these two cells; the verdict below accepts the inline controls and says so;
- the logged lines are copied from the log in full; tests named `…Reported` and the capped-settlement line log rows
  that are reported, not asserted (spec §9.1 M-ADV3-move): their positive values are collected apart as disclosed
  tails.
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
# The registered plan (internal/reviews/2026-11-19-s27-p6/plan.md) exempts N3 only from a separate control test; the
# N1/N2 inline checks were accepted after the data (S27P6F-02). The verdict follows the registered rule (s28 panel D1).
REGISTERED_INLINE = ('M_N3',)
# Tests whose standalone integer log lines are the coalition's net and are not asserted <= 0 (s28 panel D2).
NET_LOG_TESTS = {'M_N1': ('parkingWhileTheCacheLagsIsBoundedByItsInRangeTime',)}
PY_CELLS = {   # earlier configurations; their source records are not public (reported, not gated)
    'lag_leak_L1_L2': '0.000% / 0.000% NAV/yr at sigma 60 / 120% (bounds 0.1 / 0.6), model/cells/m2d_rate.py on '
                      'held-out seeds 300-499, 600 cycles per sigma, earlier configuration (before EDGE_DELAY 12 GRID); '
                      'status-quo control 8.31% / 37.2%',
    'grief_L4': 'griefer net > 0 on 0 rows, model/cells/regate_l4_l9.py on held-out seeds 300-499, earlier '
                'configuration; control without re-alignment profits',
    'adv3_phi_scaled': 'ADV-3 under a phi = 10% charge earns about 10x base (control); under the 100% charge max net '
                       '-2.9e9 wei; design-phase cell, seeds of that run not public',
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
            c['logs'][rest] = lines
        if rest.endswith('Reported') or any('disclosed tail' in x for x in lines):
            vals = []
            for j, x in enumerate(lines):
                if rest.endswith('Reported') or 'disclosed tail' in x:
                    vals += [int(v) for v in re.findall(r'(?<![\w.])-?\d{6,}(?![\w.])', x)]
            pos = [v for v in vals if v > 0]
            if pos:
                c.setdefault('reported_unasserted_tails_wei', {})[rest] = max(pos)
        if rest in NET_LOG_TESTS.get(cell, ()):
            pos = [int(x) for x in lines if re.fullmatch(r'-?\d+', x) and int(x) > 0]
            if pos:
                c.setdefault('reported_unasserted_tails_wei', {})[rest] = max(pos)
    for cell, c in cells.items():
        has_ctl = any(x['passed'] for x in c['controls'])
        c['positive_control'] = 'control test' if has_ctl else (('inline: ' + INLINE[cell]) if cell in INLINE
                                                                 else 'no positive control')
        c['ok'] = c['passed'] == c['tests'] and c['positive_control'] != 'no positive control'
        c['ok_registered'] = c['passed'] == c['tests'] and (has_ctl or cell in REGISTERED_INLINE)
        ok &= c['ok']
    ok_reg = bool(tests) and all(c['ok_registered'] for c in cells.values())
    failing_reg = sorted(k for k, c in cells.items() if not c['ok_registered'])
    res = {
        'id': 'E5', 'question': 'every attack cell', 'pass_rule': '<= 0, with a positive control > 0',
        'verdict': 'PASS' if ok_reg else 'FAIL',
        'fail_reason': ('control form: ' + ', '.join(failing_reg) + ' have inline sensitivity checks, not a separate '
                        'test that profits with the defence removed (the registered plan exempted N3 only); every '
                        'asserted attack row of every cell nets <= 0') if not ok_reg else None,
        'amended_rule_verdict': 'PASS' if ok else 'FAIL',
        'amended_after_data': True,
        'verdict_rule': 'the registered rule (s28 owner-delegated panel D1); amended_rule_verdict applies the rule as '
                        'amended after the data (inline controls accepted for N1-N3, S27P6F-02)',
        'rule_applied': 'registered: every asserted attack row of every cell nets <= 0 (its test passes) and every cell '
                        'has a passing control test that profits with the defence removed (N3, a forgery cell, may use '
                        'its inline discrimination check); amended after the data: N1 and N2 inline checks accepted too',
        'rule_deviation': 'the registered plan exempted N3 only; N1 and N2 inline controls were accepted after the data '
                          '(fix review S27P6F-02); under the registered rule E5 is FAIL on N1 and N2',
        'disclosed_unasserted_tails': 'M-ADV3-move reports rows it does not assert (spec §9.1): with the settlement capped '
                                      '(a markout past the cap break-even) a coalition nets up to +0.93 ETH; with a '
                                      'protocol fee on, +0.004 ETH; off-leg stops and both-worlds overshoot up to +0.0006 '
                                      'ETH; M-N1 lane B (parking at the edge while the cached reference lags in range) nets up to +2.3e14 '
                                      'token1 wei per exit, bounded by GRID*(1+ceil(REF_WINDOW/GRID_SEC)) blocks of '
                                      'tau_v credit (2*GRID on the mainnet and DEMO tiers), asserted as a credit bound '
                                      'only (see cells.*.reported_unasserted_tails_wei)',
        'data': 'foundry (the shipped contracts on a local pool)',
        'cells': cells, 'python_cells_reported': PY_CELLS,
        'forge_log': {'path': log, 'sha256': sha(log)},
        'limits': 'M-ADV3-move and M-N1 lane B have disclosed, unasserted positive tails (see disclosed_unasserted_tails); the cells are scripted attacks (with searched parameters) on a local pool, not an exhaustive search; '
                  'the Python cells come from earlier configurations and are reported, not gated; MK-1 (a single '
                  'builder holding the reference over the markout window) is outside the model',
        'commit_public': git('rev-parse', 'HEAD'),
        'commit_internal': git('rev-parse', 'HEAD', cwd=os.path.join(ROOT, 'internal')),
    }
    t = json.dumps(res, indent=1, allow_nan=False) + '\n'
    if out:
        open(os.path.join(ROOT, out), 'w').write(t)
    print(t)
    return 0 if ok_reg else 1


if __name__ == '__main__':
    sys.exit(main(sys.argv[1:]))
