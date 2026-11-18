#!/usr/bin/env python3
"""U-IC supplement (reported, not gated; s23 fix review S23U-01): the observer environment's comparisons of the one
U-IC run, recomputed from its saved per-run rows. No re-run: the rows are pinned by their sha256.

In the observer environment a third party calls `refreshRef` every 2·GRID. `incentives.gate` (the gated rule) pairs
only lazy-under-ladder there; this supplement pairs every gated bot except lazy (which has no observer run), at each
simulated m, with the same pairing (`incentives.paired`, env=True) and the same one-sided 95% CI (`common.mean_ci`).

Usage: python3 script/uic_observer.py            # writes eval/supplements/U-IC-observer.json
       python3 script/uic_observer.py --check    # the committed supplement is complete (no rows needed)
"""
import hashlib
import json
import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, 'model', 'cells'))
ROWS = os.path.join(ROOT, 'output', 'uic', 'uic-500-200.json')
ROWS_SHA256 = '4f7446e7ebf89b8e'          # prefix of the rows written by the single U-IC run (s23)
OUT = os.path.join(ROOT, 'eval', 'supplements', 'U-IC-observer.json')
MS = (0.0, 0.01, 0.022)
BUCKETS = ('gbm-40', 'gbm-60', 'gbm-120', 'hover-60')


def bots():
    import bots as B
    return [b for b in B.GATED if b != 'lazy']


def build():
    import incentives as I
    from common import mean_ci
    raw = open(ROWS, 'rb').read()
    h = hashlib.sha256(raw).hexdigest()
    assert h.startswith(ROWS_SHA256), f'rows {h[:16]} are not the U-IC run ({ROWS_SHA256})'
    rows = json.loads(raw)['rows']
    I.MIMIC = 'edge-6h'
    ix = I.index(rows)
    seeds = sorted({r['seed'] for r in rows})
    out = {}
    for b in BUCKETS:
        out[b] = {}
        for bot in bots():
            per_m = {}
            for m in MS:
                d = [mim - x for x, mim in (I.paired(ix, b, s, bot, m, True) for s in seeds)]
                mu, lo, _ = mean_ci(d)
                per_m[f'{m:g}'] = dict(diff=mu, ci_lo=lo, n=len(d), ok=lo > 0)
            out[b][bot] = per_m
    fails = [f'{b}: mimic > {bot} at m 2.2% (diff {v["0.022"]["diff"]:+.4f}, CI lo {v["0.022"]["ci_lo"]:+.4f})'
             for b, x in out.items() for bot, v in x.items() if not v['0.022']['ok']]
    res = dict(eval='U-IC observer environment (supplement)', status='reported, not gated',
               source=dict(rows='output/uic/uic-500-200.json', sha256=h, run='eval/results/U-IC.json (single run, no re-run)'),
               mimic='edge-6h', environment='a third party calls refreshRef every 2·GRID; mimic and bot both in it',
               failing_at_2_2pct=fails, comparisons=out,
               note='U-IC.json l3_form.*.observer_m_need = Infinity for hover-60 is a bisection artifact (the '
                    'extrapolated segment above 2.2% dips below 0); every hover observer comparison passes at each '
                    'simulated m (this file)')
    os.makedirs(os.path.dirname(OUT), exist_ok=True)
    with open(OUT, 'w') as fh:
        json.dump(res, fh, indent=1, allow_nan=False)
        fh.write('\n')
    print('\n'.join(fails) or 'no failing observer comparison at 2.2%')


def check(path=OUT):
    """Problems with a supplement: every bucket × gated bot (lazy excepted) × simulated m has a row."""
    r = json.load(open(path))
    bad = []
    for b in BUCKETS:
        for bot in bots():
            for m in MS:
                if f'{m:g}' not in r.get('comparisons', {}).get(b, {}).get(bot, {}):
                    bad.append(f'{b} {bot} {m:g}')
    return bad


if __name__ == '__main__':
    if sys.argv[1:] == ['--check']:
        bad = check()
        print('\n'.join(bad) or 'complete')
        sys.exit(1 if bad else 0)
    build()
