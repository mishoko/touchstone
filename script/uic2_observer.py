#!/usr/bin/env python3
"""U-IC2 supplement (reported, not gated; spec §10 "U-IC2 frozen form", "Reported, not gated"): the observer
environment's comparisons of the one U-IC2 run, for both mimics (edge-12h, U-IC2's; edge-6h, U-IC's), recomputed from
its saved per-run rows. No re-run: the rows are pinned by their sha256.

Why a supplement: the frozen form lists "the observer environment for both mimics in every bucket" as reported, but
the harness's table (`uic2.observer_table`, from `incentives.gate`) holds only lazy-under-ladder, the one bot the gated
rule pairs in that environment; the gap was found after the run (s24) and this script, the same method as U-IC's
supplement (`script/uic_observer.py`, s23), produces the item from the same rows. It changes no gated number.

In the observer environment a third party calls `refreshRef` every 2·GRID. Every gated bot except lazy (no observer
run) is paired with the mimic at each simulated m (`incentives.paired`, env=True; one-sided 95% CI, `common.mean_ci`).

Usage: python3 script/uic2_observer.py            # writes eval/supplements/U-IC2-observer.json
       python3 script/uic2_observer.py --check    # the committed supplement is complete (no rows needed)
"""
import hashlib
import json
import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, 'model', 'cells'))
ROWS = os.path.join(ROOT, 'output', 'uic2', 'uic2-700-400.json')
ROWS_SHA256 = '97580e5c3fde1a58'          # prefix of the rows written by the single U-IC2 verdict (s24)
OUT = os.path.join(ROOT, 'eval', 'supplements', 'U-IC2-observer.json')
MS = (0.0, 0.01, 0.022)
BUCKETS = ('gbm-40', 'gbm-60', 'gbm-120', 'hover-60')
MIMICS = ('edge-12h', 'edge-6h')


def bots():
    import bots as B
    return [b for b in B.GATED if b != 'lazy']


def build():
    import incentives as I
    from common import mean_ci
    raw = open(ROWS, 'rb').read()
    h = hashlib.sha256(raw).hexdigest()
    assert h.startswith(ROWS_SHA256), f'rows {h[:16]} are not the U-IC2 run ({ROWS_SHA256})'
    rows = json.loads(raw)['rows']
    seeds = sorted({r['seed'] for r in rows})
    out, fails = {}, {}
    for mimic in MIMICS:
        I.MIMIC = mimic
        ix = I.index(rows)
        out[mimic], fails[mimic] = {}, []
        for b in BUCKETS:
            out[mimic][b] = {}
            for bot in bots():
                per_m = {}
                for m in MS:
                    d = [mim - x for x, mim in (I.paired(ix, b, s, bot, m, True) for s in seeds)]
                    mu, lo, _ = mean_ci(d)
                    per_m[f'{m:g}'] = dict(diff=mu, ci_lo=lo, n=len(d), ok=lo > 0)
                out[mimic][b][bot] = per_m
                v = per_m['0.022']
                if not v['ok']:
                    fails[mimic].append(f'{b}: {mimic} > {bot} at m 2.2% (diff {v["diff"]:+.4f}, CI lo {v["ci_lo"]:+.4f})')
    res = dict(eval='U-IC2 observer environment (supplement)', status='reported, not gated',
               source=dict(rows='output/uic2/uic2-700-400.json', sha256=h,
                           run='eval/results/U-IC2.json (single run, no re-run)',
                           backup=f'~/backups/touchstone-s24-uic2-rows-{h[:16]}.json.gz'),
               why=('the frozen form lists the observer environment for both mimics as reported; the harness table '
                    'holds only lazy-under-ladder (the one bot the gated rule pairs there); produced after the run from '
                    'the same rows by the method of script/uic_observer.py (s23)'),
               environment='a third party calls refreshRef every 2·GRID; mimic and bot both in it',
               failing_at_2_2pct=fails, comparisons=out)
    os.makedirs(os.path.dirname(OUT), exist_ok=True)
    with open(OUT, 'w') as fh:
        json.dump(res, fh, indent=1, allow_nan=False)
        fh.write('\n')
    for mimic in MIMICS:
        print(f'{mimic}: ' + ('; '.join(fails[mimic]) or 'no failing observer comparison at 2.2%'))


def check(path=OUT):
    """Problems with a supplement: every mimic × bucket × gated bot (lazy excepted) × simulated m has a row."""
    r = json.load(open(path))
    bad = []
    for mimic in MIMICS:
        for b in BUCKETS:
            for bot in bots():
                for m in MS:
                    if f'{m:g}' not in r.get('comparisons', {}).get(mimic, {}).get(b, {}).get(bot, {}):
                        bad.append((mimic, b, bot, m))
    return bad


if __name__ == '__main__':
    if sys.argv[1:] == ['--check']:
        bad = check()
        print('U-IC2 observer supplement', 'complete' if not bad else f'incomplete: {bad[:3]}')
        sys.exit(1 if bad else 0)
    build()
