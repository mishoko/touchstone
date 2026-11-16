#!/usr/bin/env python3
"""Size budget record: every deployed contract and linked library under src/ has an accepted runtime size in
`audits/size-budget.json`; a build whose runtime grew by more than `growth_limit` bytes over the accepted size fails
until the record is updated with a note that explains the growth. The hard 22,000 B gate is `script/size_gate.py`.

  python3 script/size_budget.py                       # check (CI)
  python3 script/size_budget.py --write --note TEXT   # accept the current sizes (the note is required)
  python3 script/size_budget.py --selftest

Every contract of src/ in the size report must have an entry (a new contract fails until it is recorded). The vault
also carries `vault_rows`: the module rows of the size table that the current build implements, with their budgets;
the check prints the measured vault against their sum.
"""
import argparse, json, os, subprocess, sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
REC = os.path.join(ROOT, 'audits', 'size-budget.json')
sys.path.insert(0, os.path.join(ROOT, 'script'))
from size_gate import declared  # noqa: E402


def measure():
    r = subprocess.run(['forge', 'build', '--sizes', '--json'], cwd=ROOT, capture_output=True, text=True)
    if r.returncode != 0:
        print(r.stdout[-2000:], r.stderr[-2000:], sep='\n')
        raise SystemExit(f'size budget: forge exited {r.returncode}')
    report = json.loads(r.stdout)
    return {n: report[n]['runtime_size'] for n in sorted(declared('src')) if n in report}


def check(rec, sizes):
    """Returns failure strings: growth above the limit, a missing entry, or a recorded contract that disappeared."""
    errs, lim = [], rec['growth_limit']
    for n, s in sizes.items():
        e = rec['contracts'].get(n)
        if e is None:
            errs.append(f'{n}: {s} B is not in {os.path.basename(REC)} (record it with --write --note)')
        elif s - e['runtime'] > lim:
            errs.append(f'{n}: {s} B grew {s - e["runtime"]} B over the accepted {e["runtime"]} B (> {lim} B); '
                        'accept it with --write --note explaining the growth')
    for n in rec['contracts']:
        if n not in sizes:
            errs.append(f'{n}: recorded but not built')
    return errs


def selftest():
    rec = {'growth_limit': 256, 'contracts': {'A': {'runtime': 1000}, 'B': {'runtime': 500}}}
    ok = not check(rec, {'A': 1256, 'B': 100})
    ok &= bool(check(rec, {'A': 1257, 'B': 500}))
    ok &= bool(check(rec, {'A': 1000, 'B': 500, 'C': 1}))
    ok &= bool(check(rec, {'A': 1000}))
    print('size budget selftest', 'OK' if ok else 'FAILED')
    return 0 if ok else 1


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--write', action='store_true')
    ap.add_argument('--note')
    ap.add_argument('--selftest', action='store_true')
    a = ap.parse_args()
    if a.selftest:
        return selftest()
    rec = json.load(open(REC))
    sizes = measure()
    if a.write:
        if not a.note:
            print('size budget: --write needs --note'); return 1
        for n, s in sizes.items():
            e = rec['contracts'].setdefault(n, {'runtime': s, 'history': []})
            if e.get('runtime') != s or not e.get('history'):
                e.setdefault('history', []).append({'runtime': s, 'note': a.note})
            e['runtime'] = s
        for n in [n for n in rec['contracts'] if n not in sizes]:
            del rec['contracts'][n]
        open(REC, 'w').write(json.dumps(rec, indent=1, ensure_ascii=False) + '\n')
        print(f'size budget: recorded {len(sizes)} contracts')
    errs = check(rec, sizes)
    for n, s in sizes.items():
        print(f'  {n:24s} {s:6d} B (accepted {rec["contracts"].get(n, {}).get("runtime", "-")})')
    rows = rec.get('vault_rows', [])
    if rows and 'TouchstoneVault' in sizes:
        budget = sum(r['budget'] for r in rows)
        un = rec.get('vault_unattributed', 0)
        over = sizes['TouchstoneVault'] - budget
        print(f'  vault vs the rows it implements: {sizes["TouchstoneVault"]} B measured, {budget} B budgeted '
              f'({len(rows)} rows), {over:+d} B; unattributed allowance {un} B '
              f'({"within" if over <= un else "OVER (reported; the hard gate is size_gate.py)"})')
    for e in errs:
        print('FAIL', e)
    print('size budget', 'FAIL' if errs else 'OK')
    return 1 if errs else 0


if __name__ == '__main__':
    sys.exit(main())
