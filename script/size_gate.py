#!/usr/bin/env python3
"""Size gate: every contract and library declared under the scope directory must have a runtime of at most 22,000 B.

Runs `forge build --sizes --json` and parses its output strictly: a non-zero forge exit, output that is not a JSON
object, an entry without an integer `runtime_size`, or a contract of the scope missing from the report all fail
(exit 1), as does any runtime above the limit. Contracts outside the scope (dependencies, test helpers) are reported
by forge but not gated.

  python3 script/size_gate.py                                  # gate src/
  FOUNDRY_PROFILE=oversized python3 script/size_gate.py --scope test/fixtures   # the fixture; must exit 1
"""
import argparse, json, os, re, subprocess, sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
LIMIT = 22_000
DECL = re.compile(r'^\s*(?:abstract\s+)?(contract|library)\s+([A-Za-z_]\w*)', re.M)


def declared(scope):
    names = set()
    for d, _, fs in os.walk(os.path.join(ROOT, scope)):
        for f in fs:
            if f.endswith('.sol'):
                src = open(os.path.join(d, f)).read()
                src = re.sub(r'//[^\n]*|/\*.*?\*/', '', src, flags=re.S)
                names |= {m.group(2) for m in DECL.finditer(src) if not re.search(
                    r'abstract\s+contract\s+' + m.group(2) + r'\b', src)}
    return names


def gate(report, names, limit):
    """Returns a list of failure strings for a parsed `--sizes --json` report."""
    if not isinstance(report, dict) or not report:
        return ['size report is not a non-empty JSON object']
    errs = []
    for k, v in report.items():
        if not isinstance(v, dict) or not isinstance(v.get('runtime_size'), int):
            errs.append(f'malformed entry: {k}')
    for n in sorted(names):
        if n not in report:
            errs.append(f'{n}: missing from the size report')
        elif isinstance(report[n], dict) and isinstance(report[n].get('runtime_size'), int) \
                and report[n]['runtime_size'] > limit:
            errs.append(f'{n}: runtime {report[n]["runtime_size"]} B > {limit} B')
    return errs


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--scope', default='src')
    ap.add_argument('--limit', type=int, default=LIMIT)
    a = ap.parse_args()
    names = declared(a.scope)
    if not names:
        print(f'size gate: no contracts under {a.scope}/'); return 1
    r = subprocess.run(['forge', 'build', '--sizes', '--json'], cwd=ROOT, capture_output=True, text=True)
    if r.returncode != 0:
        print(r.stdout[-2000:], r.stderr[-2000:], sep='\n'); print(f'size gate: forge exited {r.returncode}'); return 1
    try:
        report = json.loads(r.stdout)
    except json.JSONDecodeError as e:
        print(f'size gate: cannot parse the size report ({e})'); return 1
    errs = gate(report, names, a.limit)
    for n in sorted(names):
        if n in report and isinstance(report[n], dict):
            print(f'  {n}: {report[n].get("runtime_size")} B')
    if errs:
        for e in errs: print('size gate FAIL:', e)
        return 1
    print(f'size gate OK ({len(names)} contracts under {a.scope}/, limit {a.limit} B)')
    return 0


if __name__ == '__main__':
    sys.exit(main())
