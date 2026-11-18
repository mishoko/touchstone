#!/usr/bin/env python3
"""Reduce a Slither JSON run to the committed record `audits/slither-results.json` (claim S-1).

Usage: python3 script/slither_record.py <slither.json> "<command>" [out]
The record keeps, per result, the detector, impact, confidence and each element's type, name, file and first/last
line, plus the Slither version, `input_sha256` (`src/**/*.sol`, `foundry.toml`, `remappings.txt` and the submodule pins)
and the raw JSON's sha256 (provenance only; machine-specific, as the raw JSON holds absolute paths), so that `test/static/test_static.py::test_S_1__…` can check that `audits/SLITHER.md`
triages exactly this record (its `Record: sha256` line) and that the record was made from the current inputs. The test
does not re-run Slither: the record is reproduced by re-running the command.
"""
import hashlib
import json
import os
import subprocess
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def input_sha256(root=ROOT):
    """The hash of what the analysis depends on: every `src/**/*.sol` (directory symlinks are refused), `foundry.toml`,
    `remappings.txt` and the checked-out commit of every `lib/` submodule."""
    h = hashlib.sha256()
    for d, ds, fs in sorted(os.walk(os.path.join(root, 'src'))):
        assert not any(os.path.islink(os.path.join(d, x)) for x in ds), 'a directory symlink under src/'
        for f in sorted(fs):
            if f.endswith('.sol'):
                p = os.path.join(d, f)
                h.update(os.path.relpath(p, root).encode() + b'\0')
                h.update(open(p, 'rb').read())
    for f in ('foundry.toml', 'remappings.txt'):
        p = os.path.join(root, f)
        h.update(f.encode() + b'\0' + (open(p, 'rb').read() if os.path.exists(p) else b''))
    sub = subprocess.run(['git', '-C', root, 'submodule', 'status'], capture_output=True, text=True, check=True).stdout
    for line in sorted(sub.splitlines()):
        h.update(' '.join(line[1:].split()[:2]).encode() + b'\0')   # "<sha> <path>"
    return h.hexdigest()


def results_sha256(results):
    """The hash of the recorded results list; `audits/SLITHER.md` states it, so a regenerated record forces a re-triage."""
    return hashlib.sha256(json.dumps(results, sort_keys=True).encode()).hexdigest()[:16]


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
    rec = dict(tool=f'Slither {ver}', command=cmd, input_sha256=input_sha256(),
               raw_sha256=hashlib.sha256(open(src, 'rb').read()).hexdigest(), results=res)
    with open(out, 'w') as fh:
        json.dump(rec, fh, indent=1)
        fh.write('\n')
    print(f'{out}: {len(res)} results; inputs {rec["input_sha256"][:12]}; Record: sha256 {results_sha256(res)}')


if __name__ == '__main__':
    main()
