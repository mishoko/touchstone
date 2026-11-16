#!/usr/bin/env python3
"""Storage-layout snapshots. For every concrete contract under src/, `forge inspect <C> storageLayout --json` is
flattened (struct members expanded to absolute slots; compiler AST ids dropped) and compared with the committed
snapshot test/storage/<C>.txt. Off-chain readers decode state through `extsload`, so any layout change must be
deliberate: regenerate with --write and commit the diff.

  python3 script/storage_layout.py          # check (exit 1 on any difference or a missing snapshot)
  python3 script/storage_layout.py --write  # regenerate the snapshots
"""
import json, os, re, subprocess, sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SNAP = os.path.join(ROOT, 'test', 'storage')


def contracts():
    names = []
    for d, _, fs in os.walk(os.path.join(ROOT, 'src')):
        for f in sorted(fs):
            if f.endswith('.sol'):
                src = re.sub(r'//[^\n]*|/\*.*?\*/', '', open(os.path.join(d, f)).read(), flags=re.S)
                names += re.findall(r'^\s*contract\s+([A-Za-z_]\w*)', src, re.M)
    return sorted(set(names))


def flatten(layout):
    types = layout.get('types') or {}
    lines = []

    def walk(entries, base, prefix):
        for e in entries:
            slot = base + int(e['slot'])
            t = types[e['type']]
            name = prefix + e['label']
            lines.append(f"{slot:>4} {int(e['offset']):>2} {int(t['numberOfBytes']):>4} {name}: {t['label']}")
            if 'members' in t:
                walk(t['members'], slot, name + '.')

    walk(layout['storage'], 0, '')
    return '\n'.join(lines) + '\n'


def inspect(c):
    cmd = ['forge', 'inspect', c, 'storageLayout', '--json']
    r = subprocess.run(cmd, cwd=ROOT, capture_output=True, text=True)
    if r.returncode != 0 and 'storage layout missing from artifact' in r.stderr:
        # a build without the storage-layout output (e.g. the test build) left a stale artifact: rebuild once
        r = subprocess.run(cmd + ['--force'], cwd=ROOT, capture_output=True, text=True)
    if r.returncode != 0:
        raise SystemExit(f'storage layout: forge inspect {c} failed:\n{r.stderr[-1500:]}')
    return flatten(json.loads(r.stdout))


def main(argv):
    cs = contracts()
    if not cs:
        print('storage layout: no contracts under src/')
        return 1
    os.makedirs(SNAP, exist_ok=True)
    bad = []
    for c in cs:
        cur, p = inspect(c), os.path.join(SNAP, c + '.txt')
        if '--write' in argv:
            open(p, 'w').write(cur)
            print('wrote', os.path.relpath(p, ROOT))
        elif not os.path.exists(p):
            bad.append(f'{c}: no snapshot ({os.path.relpath(p, ROOT)})')
        elif open(p).read() != cur:
            bad.append(f'{c}: layout differs from {os.path.relpath(p, ROOT)}')
            print(cur)
    for b in bad:
        print('storage layout FAIL:', b)
    if not bad and '--write' not in argv:
        print(f'storage layout OK ({len(cs)} contracts)')
    return 1 if bad else 0


if __name__ == '__main__':
    sys.exit(main(sys.argv[1:]))
