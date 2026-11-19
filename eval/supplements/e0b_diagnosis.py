#!/usr/bin/env python3
"""E0b diagnosis (after the verdict; reported only, the verdict is unchanged): the residual per held-out script with the
passive fee leg L_b*dtau_b*f_w replaced by its true value, L_b*(dg0*P(p1) + dg1)/2^128, from the witness range's fee
growth read after the opening and before the closing vault call (`E0b-<tag>-witness.jsonl` pre/post rows).

  python3 eval/supplements/e0b_diagnosis.py <held-out data dir>   # checks the numbers in eval/supplements/E0b-H.json
"""
import json, os, sys
HERE = os.path.dirname(os.path.abspath(__file__))
EVAL = os.path.dirname(HERE)
sys.path.insert(0, EVAL)
import E0a as A  # noqa: E402
import E0b as B  # noqa: E402


def worst_with_true_passive(d, tag, tier):
    raw = B.load(f'{d}/E0b-{tag}-logs.jsonl')
    truth = B.load(f'{d}/E0b-{tag}-truth.jsonl')
    fr = B.load(f'{d}/E0b-{tag}-feereads.jsonl')
    W = {(r['c'], r['kind']): r for r in B.load(f'{d}/E0b-{tag}-witness.jsonl') if r['kind'] in ('pre', 'post')}
    T = {x['c']: x for x in truth}
    led, meta = B.ledger(raw, tier, fr)
    for s in meta['segs']:
        if s['L'] == 0 or s.get('unresolved'):
            continue
        seg, a, z = T[s['close']]['seg'], W.get((s['open'], 'post')), W.get((s['close'], 'pre'))
        if not (a and z and seg):
            continue
        d0 = (int(z['g0']) - int(a['g0'])) % B.M256
        d1 = (int(z['g1']) - int(a['g1'])) % B.M256
        s['fee'] = s['feeV'] - s['Lb'] * (d0 * A.price(s['p1']) + d1) / B.Q128
        s['alloc'] = s['price'] + s['fee']
    return max(abs(x['residual_bp']) for x in A.identity(led, truth) if not x['unresolved'])


def main(d):
    tier = {k: int(v) for k, v in json.load(open(f'{d}/E0b-tier.json')).items()}
    pub = json.load(open(os.path.join(HERE, 'E0b-H.json')))['diagnosis']['worst_bp_with_true_passive_leg']
    ok = True
    for tag, v in sorted(pub.items()):
        w = round(worst_with_true_passive(d, tag, tier), 4)
        ok &= w == v
        print(tag, w, 'matches' if w == v else f'DIFFERS from {v}')
    return 0 if ok else 1


if __name__ == '__main__':
    sys.exit(main(sys.argv[1]))
