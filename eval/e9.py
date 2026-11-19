#!/usr/bin/env python3
"""E9 (spec §10): the regime table behind "positioning is not paid".

Zero-skill (random) curators paid by carry-based benchmarks across three fee/LVR regimes, from the benchmark-design
simulator of the design phase (outputs in `eval/supplements/E9/`; the simulator scripts are design-phase spike code
and are not part of this repository). The result is a property of the benchmark designs, not of Touchstone's
contracts: it supports the choice to pay a base fee for delivered exposure and charge execution, rather than pay any
in-range-carry benchmark. Verdict REPORTED; the match to the published table (2 significant figures) is a
consistency check of the transcription only.

  python3 eval/e9.py [--out eval/results/E9.json]
"""
import argparse, hashlib, json, os, re, subprocess, sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SUP = os.path.join('eval', 'supplements', 'E9')
REGIMES = [('carry+', 'carry+'), ('balanced', 'bal'), ('LVR>fees', 'lvr>fee')]
# design label -> simulator rule key; the published table (random curator, pay above base, %)
DESIGNS = {'Mandate benchmark': ('rep', [40, 6, -13]), 'Exposure-matched': ('b_seg', [0.1, 0.1, -0.9])}
IDLE_TABLE = [-37.6, 3.4]   # "an idle curator lands anywhere from -37.6x to +3.4x base" (phi_a = 10%)


def sha(p):
    return hashlib.sha256(open(os.path.join(ROOT, p), 'rb').read()).hexdigest()


def git(*a, cwd=ROOT):
    return subprocess.run(['git', *a], cwd=cwd, capture_output=True, text=True).stdout.strip()


def sig2(x):
    return float(f'{x:.2g}')


def main(argv):
    ap = argparse.ArgumentParser()
    ap.add_argument('--out', default=None)
    a = ap.parse_args(argv)
    cand = json.load(open(os.path.join(ROOT, SUP, 'cand_w24.json')))
    rows, match = {}, True
    for name, (key, table) in DESIGNS.items():
        rows[name] = {}
        for (label, rk), t in zip(REGIMES, table):
            pay, se = cand[f'{rk}/random'][key][3], cand[f'{rk}/random'][key][4]
            pct, pct_se = round(100 * pay, 2), round(100 * se, 2)
            rows[name][label] = {'pay_above_base_pct': pct, 'se_pct': pct_se, 'table': t}
            match &= sig2(100 * pay) == sig2(t) if abs(t) >= 1 else round(100 * pay, 1) == t
    idle = {}
    for line in open(os.path.join(ROOT, SUP, 'alloc_out.txt')):
        m = re.match(r'(\S+)/idle\s.*phi_a=10%: pay/base-1\s+([-+]?\d+\.\d+)', line)
        if m:
            idle[m.group(1)] = float(m.group(2))
    lo, hi = min(idle.values()), max(idle.values())
    match &= [round(lo, 1), round(hi, 1)] == IDLE_TABLE
    res = {
        'id': 'E9',
        'question': 'the D1 regime table: zero-skill curators paid by a carry benchmark across regimes (the derivation '
                    'behind "positioning is not paid")',
        'pass_rule': 'reported; matches the published design table',
        'verdict': 'REPORTED',
        'data': 'design-phase benchmark simulator',
        'scope': 'a property of carry-based benchmark designs, not of the Touchstone contracts; it supports the design '
                 'choice (no benchmark built from in-range carry pays for positioning without paying zero-skill '
                 'curators for the regime)',
        'numbers': {'random_curator_pay_above_base': rows,
                    'idle_curator_pay_over_base_phi_a_10pct': idle,
                    'idle_range_x_base': [lo, hi]},
        'consistency_with_published_table': match,
        'evidence': {p: sha(os.path.join(SUP, p)) for p in ('cand_w24.json', 'cand_w24.txt', 'alloc_out.txt')},
        'limits': 'design-phase simulator (24-segment trailing window, three stylised regimes); the simulator scripts are '
                  'not published, only their outputs; not re-run (the table is reported, not re-measured)',
        'commit_public': git('rev-parse', 'HEAD'),
        'commit_internal': git('rev-parse', 'HEAD', cwd=os.path.join(ROOT, 'internal')),
    }
    txt = json.dumps(res, indent=1, allow_nan=False) + '\n'
    if a.out:
        open(os.path.join(ROOT, a.out), 'w').write(txt)
    print(txt)
    return 0 if match else 1


if __name__ == '__main__':
    sys.exit(main(sys.argv[1:]))
