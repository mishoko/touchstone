#!/usr/bin/env python3
"""E2 (spec §10): realised action loss vs charge for the honest (mimic-shaped) placement.

Pre-registered in the s27 P6 plan (design review S27P6D-01/02/14), fixed before any E2 data existed:
- the cell: `model/cells/regate_l4_l9.py --l9-only` (row L9): single honest exit-state placements (edge60 range
  shape, the bundler re-aligns spot to the reference pool's price), the expected charge over 400 window-error draws
  vs the depositors' realised loss; GBM states; seeds 1108-1307 at sigma 60% and 120%;
- gate column `c_fa` (the shipped fee-aware settlement): a row is short iff realised > c_fa + 1 wei; PASS iff 0
  short rows at both sigma. No tolerance;
- reported, not gated: C120's short count, the max shortfall (wei and bp of NAV), the overcharge mean and p99
  (bp per placement);
- the RNG keys the cell uses for seeds 1108-1307 are asserted disjoint from those of seeds 0-1107.

  python3 eval/e2.py [procs] [--out eval/results/E2.json]
"""
import hashlib, json, os, subprocess, sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
FIRST, END = 1108, 1308
SIGMAS = (0.6, 1.2)
RAW = os.path.join(ROOT, 'output', 'e2')


def keys(seed):
    """Every RNG key `regate_l4_l9.l9` derives from a seed (l9, eta_honest, lags, sample_state -> gbm_ticks)."""
    out = set()
    for sg in SIGMAS:
        out |= {('l9', seed * 31 + int(sg * 100)), ('eta', 5_000_011 + seed * 17 + int(sg * 100)),
                ('lags', 77_000_003 * seed + int(sg * 1000)), ('gbm', seed * 1000 + int(sg * 100))}
    return out


def sha(p):
    return hashlib.sha256(open(p, 'rb').read()).hexdigest()


def git(*a, cwd=ROOT):
    return subprocess.run(['git', *a], cwd=cwd, capture_output=True, text=True).stdout.strip()


def pct(xs, q):
    xs = sorted(xs)
    return xs[min(len(xs) - 1, int(q * len(xs)))]


def main(argv):
    procs = argv[0] if argv and argv[0].isdigit() else '4'
    out_path = argv[argv.index('--out') + 1] if '--out' in argv else None
    fresh = set().union(*(keys(s) for s in range(FIRST, END)))
    spent = set().union(*(keys(s) for s in range(0, FIRST)))
    assert not fresh & spent, 'E2 seeds share an RNG key with seeds 0-1107'
    raw = os.path.join(RAW, f'l9_{FIRST}-{END - 1}.json')
    assert not os.path.exists(raw), f'{raw} exists: E2 runs once'
    subprocess.run([sys.executable, os.path.join(ROOT, 'model', 'cells', 'regate_l4_l9.py'), f'{FIRST}:{END}', procs,
                    '--l9-only', '--out', RAW], check=True)
    d = json.load(open(raw))
    r9 = d['r9']
    bp = lambda x, n: 1e4 * x / n  # noqa: E731
    per, ok = {}, True
    for sg in SIGMAS:
        g = [r for r in r9 if r['sigma'] == sg]
        short = [r for r in g if r['real'] > r['c_fa'] + 1]
        short_c120 = [r for r in g if r['real'] > r['ce'] + 1]
        over = [bp(r['c_fa'] - r['real'], r['nav']) for r in g]
        ok &= len(g) > 0 and not short
        per[f'sigma_{int(sg * 100)}'] = dict(
            placements=len(g), short_fee_aware=len(short),
            max_shortfall_wei=max([r['real'] - r['c_fa'] for r in short], default=0),
            max_shortfall_bp=max([bp(r['real'] - r['c_fa'], r['nav']) for r in short], default=0),
            short_c120_reported=len(short_c120),
            overcharge_mean_bp=round(sum(over) / len(over), 6), overcharge_p99_bp=round(pct(over, 0.99), 6))
    res = {
        'id': 'E2', 'question': 'mimic: realized action loss vs charge',
        'pass_rule': 'charge >= realized loss; overcharge from reference lag reported',
        'verdict': 'PASS' if ok else 'FAIL',
        'rule_applied': 'fee-aware settlement (c_fa): 0 rows with realised > c_fa + 1 wei at both sigma',
        'data': 'gbm-synthetic',
        'cell': 'single-placement cell (edge60 range shape, honest flow): the expected charge over 400 window-error '
                'draws vs the realised loss',
        'seeds': [FIRST, END - 1], 'sigmas': list(SIGMAS), 'numbers': per,
        'raw': {'path': os.path.relpath(raw, ROOT), 'sha256': sha(raw), 'meta': d.get('meta')},
        'limits': 'not path-level (one sampled exit state per seed and sigma), not the edge-12h policy, the charge in '
                  'expectation over the window error (no per-draw shortfall), GBM, not replay',
        'commit_public': git('rev-parse', 'HEAD'),
        'commit_internal': git('rev-parse', 'HEAD', cwd=os.path.join(ROOT, 'internal')),
    }
    txt = json.dumps(res, indent=1, allow_nan=False, default=str) + '\n'
    if out_path:
        open(os.path.join(ROOT, out_path), 'w').write(txt)
    print(txt)
    return 0 if ok else 1


if __name__ == '__main__':
    sys.exit(main(sys.argv[1:]))
