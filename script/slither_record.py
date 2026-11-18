#!/usr/bin/env python3
"""Reduce a Slither JSON run to the committed record `audits/slither-results.json` (claim S-1).

Usage: python3 script/slither_record.py <slither.json> "<command>" [out]
The record keeps, per result, the detector, impact, confidence and each element's type, name, file and first/last
line, plus the Slither version and `src_sha256` (the hash of every `src/**/*.sol` the run analysed), so that
`test/static/test_static.py::test_S_1__…` can check that `audits/SLITHER.md` triages exactly this run and that the run
is of the current `src/`.
"""
import hashlib
import json
import os
import subprocess
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def src_sha256(root=ROOT):
    h = hashlib.sha256()
    for d, _, fs in sorted(os.walk(os.path.join(root, 'src'))):
        for f in sorted(fs):
            if f.endswith('.sol'):
                p = os.path.join(d, f)
                h.update(os.path.relpath(p, root).encode() + b'\0')
                h.update(open(p, 'rb').read())
    return h.hexdigest()


def main():
    src, cmd = sys.argv[1], sys.argv[2]
    out = sys.argv[3] if len(sys.argv) > 3 else os.path.join(ROOT, 'audits', 'slither-results.json')
    d = json.load(open(src))
    assert d.get('success') is True, 'the Slither run did not succeed'
    ver = subprocess.run(['slither', '--version'], capture_output=True, text=True).stdout.strip()
    res = []
    for r in d['results']['detectors']:
        els = []
        for e in r['elements']:
            sm = e.get('source_mapping', {})
            ln = sm.get('lines') or [0]
            els.append(dict(type=e['type'], name=e['name'], file=sm.get('filename_relative'), lines=[ln[0], ln[-1]]))
        res.append(dict(check=r['check'], impact=r['impact'], confidence=r['confidence'], elements=els))
    res.sort(key=lambda r: (r['impact'], r['check'], json.dumps(r['elements'])))
    rec = dict(tool=f'Slither {ver}', command=cmd, src_sha256=src_sha256(), results=res)
    with open(out, 'w') as fh:
        json.dump(rec, fh, indent=1)
        fh.write('\n')
    print(f'{out}: {len(res)} results, src {rec["src_sha256"][:12]}')


if __name__ == '__main__':
    main()
