#!/usr/bin/env python3
"""Claims coverage. Every claim ID listed in test/CLAIMS.md that is due at or before the current phase must be covered
by at least one test function named `test_<ID>__<claim>` or `invariant_<ID>__<claim>` under test/ (Solidity or
Python), with `-` replaced by `_` inside the ID and a double underscore before the claim, or by an evaluation result
`eval/results/<ID>.json`. The double underscore keeps one ID's tests from covering another ID that it prefixes
(a test `test_U_clock_gap__x` does not cover `U-clock`).

  CLAIMS_PHASE=P3 python3 script/claims.py      # IDs due in P0..P3 must be covered (default: P6, all)
  python3 script/claims.py --table              # ID | due | status
  python3 script/claims.py --selftest           # naming and phase-rule fixtures
  python3 script/claims.py --claims <file>      # check another claims file (used by the tests)
  python3 script/claims.py --generate <spec>    # regenerate test/CLAIMS.md from the specification's test matrix,
                                                # evaluation table and due table

Exit 1 on a missing ID."""
import json, glob, os, re, sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
CLAIMS = os.path.join(ROOT, 'test', 'CLAIMS.md')
PHASES = ['P0', 'P1', 'P2', 'P3', 'P4', 'P5', 'P6']
ROW = re.compile(r'^\| (?P<id>[A-Z]+(?:-[A-Za-z0-9]+)*|E[0-9]+[a-z]?) \| (?P<due>P\d) \| (?P<kind>test|eval) \|$', re.M)


def pattern(i):
    return r'\b(test|invariant)_' + re.escape(i.replace('-', '_')) + r'__'


def load(path):
    """Returns [(id, due, kind)] from a claims file; fails on an empty or duplicated list."""
    rows = [(m['id'], m['due'], m['kind']) for m in ROW.finditer(open(path).read())]
    if not rows:
        raise SystemExit(f'claims: no IDs in {path}')
    ids = [r[0] for r in rows]
    dup = sorted({i for i in ids if ids.count(i) > 1})
    if dup:
        raise SystemExit('claims: duplicated IDs: ' + ' '.join(dup))
    return rows


def test_text(root):
    files = glob.glob(os.path.join(root, 'test', '**', '*.sol'), recursive=True) + \
        glob.glob(os.path.join(root, 'test', '**', '*.py'), recursive=True)
    return '\n'.join(open(p).read() for p in files)


def eval_passed(path):
    """An eval result covers its ID only when it exists and its verdict is PASS (a FAIL is an unmet claim), or when it
    is a published negative result (`verdict` FAIL, `negative_result` true, a non-empty `decision` naming the ruling)
    whose `superseded_by` eval exists in the same directory with verdict PASS. The failed result stays FAIL and is published; only the amended eval can meet
    the row."""
    if not os.path.exists(path):
        return False
    try:
        r = json.load(open(path))
        if r.get('verdict') == 'PASS':
            return True
        sup = r.get('superseded_by')
        dec = r.get('decision')
        if (r.get('verdict') == 'FAIL' and r.get('negative_result') is True and isinstance(sup, str) and sup
                and isinstance(dec, str) and dec):
            q = os.path.join(os.path.dirname(path), sup + '.json')
            return os.path.exists(q) and json.load(open(q)).get('verdict') == 'PASS'
        return False
    except (ValueError, AttributeError):
        return False


def covered(i, tests, root, kind='test'):
    """A test row is covered by a matching test name or a PASS eval result; an eval row only by a PASS result (a test
    named after an eval never covers it; s19 fix review S19F2-03)."""
    passed = eval_passed(os.path.join(root, 'eval', 'results', i + '.json'))
    if kind == 'eval':
        return passed
    return bool(re.search(pattern(i), tests)) or passed


def decision_refs_ok(root):
    """Every `decision log #N` cited by an eval result names an existing entry of the decision log that mentions the
    eval (checked only where the internal history is present; s20 fix review S20F3-01)."""
    hist = os.path.join(root, 'internal', 'spec-history.md')
    if not os.path.exists(hist):
        return []
    h = open(hist).read()
    errs = []
    for f in glob.glob(os.path.join(root, 'eval', 'results', '*.json')):
        try:
            dec = json.load(open(f)).get('decision', '')
        except (ValueError, AttributeError):
            continue
        eid = os.path.basename(f)[:-5]
        for n in re.findall(r'decision log #(\d+)', dec or ''):
            m = re.search(r'^\| ' + n + r' \|(.*)$', h, re.M)
            sup = json.load(open(f)).get('superseded_by') or eid
            if not m or sup not in m.group(1):
                errs.append(f'{eid}: decision log #{n} missing or does not mention {sup}')
    return errs


def missing(rows, tests, phase, root):
    lim = PHASES.index(phase)
    return [i for i, due, k in rows if PHASES.index(due) <= lim and not covered(i, tests, root, k)]


# ------------------------------------------------------------------ generation from the specification
def _matrix_ids(spec):
    m = re.search(r'^### [\d.]+ Matrix\n(.*?)^### ', spec, re.S | re.M)
    if not m:
        raise SystemExit('generate: test matrix not found')
    ids = []
    for cell in re.findall(r'^\| ([^|]+) \|', m.group(1), re.M):
        for i in re.split(r'[,/]', cell):
            i = i.strip()
            if re.match(r'^[A-Z]+(-[A-Za-z0-9]+)+$', i) and '*' not in i and i not in ids:
                ids.append(i)
    return ids


def _eval_ids(spec):
    return list(dict.fromkeys(re.findall(r'^\| (E[0-9]+[a-z]?|E-[A-Z]+) \|', spec, re.M)))


def _due(spec):
    parts = spec.split('**Due table')
    if len(parts) < 2:
        raise SystemExit('generate: due table not found')
    d = {}
    for ph, row in re.findall(r'^\| (P\d) \| ([^|]+) \|', parts[1], re.M):
        for i in row.split(','):
            i = i.strip()
            if i in d:
                raise SystemExit(f'generate: {i} is due twice')
            d[i] = ph
    return d


def generate(spec_path):
    spec = open(spec_path).read()
    tests, evals, d = _matrix_ids(spec), _eval_ids(spec), _due(spec)
    rows = [(i, 'test') for i in tests] + [(e, 'eval') for e in evals if e not in tests]
    nd = [i for i, _ in rows if i not in d]
    extra = [i for i in d if i not in {r[0] for r in rows}]
    if nd or extra:
        raise SystemExit('generate: no due phase: ' + ' '.join(nd) + ' | due but not listed: ' + ' '.join(extra))
    rows.sort(key=lambda r: (PHASES.index(d[r[0]]), r[1] == 'eval'))
    out = ['# Claims', '',
           'Every claim ID with the phase by which it must be covered. `script/claims.py` checks that each ID due at or',
           'before `CLAIMS_PHASE` has a test `test_<ID>__<claim>` / `invariant_<ID>__<claim>` (with `-` written as `_`',
           'inside the ID) or an evaluation result `eval/results/<ID>.json`. Generated by',
           '`python3 script/claims.py --generate <spec>`; do not edit by hand.', '',
           '| ID | Due | Kind |', '|---|---|---|']
    out += [f'| {i} | {d[i]} | {k} |' for i, k in rows]
    open(CLAIMS, 'w').write('\n'.join(out) + '\n')
    print(f'wrote {os.path.relpath(CLAIMS, ROOT)}: {len(rows)} IDs')


# ------------------------------------------------------------------ self-test
def selftest(rows):
    ids = [r[0] for r in rows]
    errs = []
    for a in ids:  # no ID's test name can satisfy another ID
        for b in ids:
            if a != b and re.search(pattern(a), 'function test_' + b.replace('-', '_') + '__x()'):
                errs.append(f'shadow: a test for {b} satisfies {a}')
    rs = [('U-clock', 'P1', 'test'), ('U-clock-gap', 'P1', 'test')]
    if missing(rs, 'function test_U_clock_gap__x()', 'P1', '/nonexistent') != ['U-clock']:
        errs.append('fixture: test_U_clock_gap__x must leave U-clock missing')
    if missing([('U-ref', 'P3', 'test')], '', 'P2', '/nonexistent'):
        errs.append('fixture: an ID due in P3 must not be required at P2')
    if missing([('S-3', 'P0', 'test')], 'def test_S_3__ok():', 'P0', '/nonexistent'):
        errs.append('fixture: a Python test must cover its ID')
    import tempfile
    with tempfile.TemporaryDirectory() as d:  # an eval result covers its ID only with verdict PASS
        os.makedirs(os.path.join(d, 'eval', 'results'))
        rp = os.path.join(d, 'eval', 'results', 'E0.json')
        json.dump({'verdict': 'FAIL'}, open(rp, 'w'))
        if missing([('E0', 'P4', 'eval')], '', 'P4', d) != ['E0']:
            errs.append('fixture: a FAIL eval result must leave its ID missing')
        if missing([('E0', 'P4', 'eval')], 'function test_E0__x()', 'P4', d) != ['E0']:
            errs.append('fixture: a test named after an eval must not cover it')
        # a published negative result meets its row only through a PASS successor
        json.dump({'verdict': 'FAIL', 'negative_result': True, 'superseded_by': 'E0a', 'decision': 'x'}, open(rp, 'w'))
        sp = os.path.join(d, 'eval', 'results', 'E0a.json')
        if missing([('E0', 'P4', 'eval')], '', 'P4', d) != ['E0']:
            errs.append('fixture: a negative result without its successor must leave its ID missing')
        json.dump({'verdict': 'FAIL'}, open(sp, 'w'))
        if missing([('E0', 'P4', 'eval')], '', 'P4', d) != ['E0']:
            errs.append('fixture: a negative result with a FAIL successor must leave its ID missing')
        json.dump({'verdict': 'PASS'}, open(sp, 'w'))
        if missing([('E0', 'P4', 'eval')], '', 'P4', d):
            errs.append('fixture: a negative result with a PASS successor must meet its ID')
        json.dump({'verdict': 'FAIL', 'negative_result': True, 'superseded_by': 'E0a'}, open(rp, 'w'))
        if missing([('E0', 'P4', 'eval')], '', 'P4', d) != ['E0']:
            errs.append('fixture: a negative result without its decision must leave its ID missing')
        json.dump({'verdict': 'FAIL', 'superseded_by': 'E0a'}, open(rp, 'w'))
        if missing([('E0', 'P4', 'eval')], '', 'P4', d) != ['E0']:
            errs.append('fixture: a FAIL not marked as a negative result must leave its ID missing')
        json.dump({'verdict': 'PASS'}, open(rp, 'w'))
        if missing([('E0', 'P4', 'eval')], '', 'P4', d):
            errs.append('fixture: a PASS eval result must cover its ID')
    if _eval_ids('| E0 | q | p |\n| E0a | q | p |\n') != ['E0', 'E0a']:
        errs.append('fixture: a suffixed eval ID (E0a) must be listed')
    if not ROW.search('| E0a | P4 | eval |'):
        errs.append('fixture: a suffixed eval ID (E0a) must parse as a claims row')
    for e in errs:
        print('claims selftest:', e)
    print('claims selftest', 'OK' if not errs else 'FAIL')
    return not errs


def main(argv):
    if '--generate' in argv:
        generate(argv[argv.index('--generate') + 1])
        return 0
    path = argv[argv.index('--claims') + 1] if '--claims' in argv else CLAIMS
    rows = load(path)
    if '--selftest' in argv:
        return 0 if selftest(rows) else 1
    phase = os.environ.get('CLAIMS_PHASE', 'P6')
    if phase not in PHASES:
        print('CLAIMS_PHASE must be one of', ' '.join(PHASES))
        return 2
    tests = test_text(ROOT)
    miss = missing(rows, tests, phase, ROOT)
    if '--table' in argv:
        for i, due, _ in rows:
            st = 'covered' if covered(i, tests, ROOT) else ('MISSING' if i in miss else 'not due')
            print(f'| {i} | {due} | {st} |')
    req = [i for i, due, _ in rows if PHASES.index(due) <= PHASES.index(phase)]
    print(f'{len(req) - len(miss)}/{len(req)} claim IDs due by {phase} covered')
    bad = decision_refs_ok(ROOT)
    for e in bad:
        print('decision reference:', e)
    if bad:
        return 1
    if miss:
        print('missing:', ' '.join(miss))
        return 1
    return 0


if __name__ == '__main__':
    sys.exit(main(sys.argv[1:]))
