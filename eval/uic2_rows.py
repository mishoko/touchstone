#!/usr/bin/env python3
"""Aggregates U-IC2's per-path rows (`output/uic2/part-700-200.json`, `part-900-200.json`; seeds 700-1099) into the
per-(bucket, bot, m, observer) means that E4 and E8 cite: net, net + gas (the net before gas), gas, charges, actions,
pings, pokes, in-range shares, reverts by kind. One part file is loaded at a time (each is about 335 MB).
The parts are U-IC2's frozen output; this script only reads them and records their sha256.

  python3 eval/uic2_rows.py [--out eval/supplements/U-IC2-agg.json]
"""
import gc, hashlib, json, os, sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
PARTS = ['output/uic2/part-700-200.json', 'output/uic2/part-900-200.json']
KEYS = ('net', 'gas', 'charges', 'actions', 'pings', 'pokes', 'f', 'f_actual', 'vested', 'bond_burn', 'bank_burn',
        'forfeit')


def sha(p):
    h = hashlib.sha256()
    with open(os.path.join(ROOT, p), 'rb') as fh:
        for b in iter(lambda: fh.read(1 << 20), b''):
            h.update(b)
    return h.hexdigest()


def main(argv):
    out = argv[argv.index('--out') + 1] if '--out' in argv else 'eval/supplements/U-IC2-agg.json'
    acc, meta = {}, []
    for p in PARTS:
        d = json.load(open(os.path.join(ROOT, p)))
        meta.append(dict(path=p, sha256=sha(p), **{k: d['meta'].get(k) for k in ('commit', 'model_hash', 'first', 'n')}))
        for r in d['rows']:
            b = f"{r['kind']}-{int(round(r['sigma'] * 100))}"
            k = (b, r['bot'], r['m'], bool(r['observer']))
            a = acc.setdefault(k, {'n': 0, 'net_plus_gas_abs_max': 0.0, 'reverts': {}, **{x: 0.0 for x in KEYS}})
            a['n'] += 1
            for x in KEYS:
                a[x] += float(r.get(x) or 0.0)
            a['net_plus_gas_abs_max'] = max(a['net_plus_gas_abs_max'], abs(float(r['net']) + float(r['gas'])))
            for kk, v in (r.get('revert_kinds') or {}).items():
                a['reverts'][kk] = a['reverts'].get(kk, 0) + v
        del d
        gc.collect()
    rows = []
    for (b, bot, m, obs), a in sorted(acc.items(), key=lambda kv: tuple(map(str, kv[0]))):
        n = a['n']
        rows.append(dict(bucket=b, bot=bot, m=m, observer=obs, n=n, net_plus_gas_abs_max=a['net_plus_gas_abs_max'],
                         reverts=a['reverts'], **{f'{x}_mean': a[x] / n for x in KEYS}))
    json.dump(dict(parts=meta, rows=rows), open(os.path.join(ROOT, out), 'w'), indent=1, allow_nan=False)
    print(f'{len(rows)} groups -> {out}')
    return 0


if __name__ == '__main__':
    sys.exit(main(sys.argv[1:]))
