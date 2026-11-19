#!/usr/bin/env python3
"""E4 (spec §10): the base-accrual identity and the bot ordering.

Assembled, not re-run (P-18): the ordering from U-IC2's published paired comparisons (`eval/results/U-IC2.json`,
held-out seeds 700-1099, m 2.2%/yr, no observer); the before-gas nets from U-IC2's per-path rows
(`eval/supplements/U-IC2-agg.json`, made by `eval/uic2_rows.py`, which records the parts' sha256); the identity from
`test_F_6__baseAmountIndependentOfSegmentation` in this session's `forge test` log.
Pass (spec): mimic net >= 0 and mimic > idle/dust/stale/wide (idle nets 0). Applied: in every bucket the mimic's mean
net >= 0 and U-IC2's paired one-sided 95% lower bounds of mimic - bot > 0 for idle, dust, stale and wide; "idle nets
0" is checked before gas (net + gas exactly 0 on every path: no placement, no base, no charge) and its after-gas net
(its daily pokes) is published next to it; the F-6 identity test passed.

  python3 eval/e4.py [--forge-log output/s27-forge-test.log] [--out eval/results/E4.json]
"""
import hashlib, json, os, re, subprocess, sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
BOTS = ('idle', 'dust', 'stale', 'wide')
MIMIC = 'edge-12h'


def sha(p):
    return hashlib.sha256(open(os.path.join(ROOT, p), 'rb').read()).hexdigest()


def git(*a, cwd=ROOT):
    return subprocess.run(['git', *a], cwd=cwd, capture_output=True, text=True).stdout.strip()


def main(argv):
    log = argv[argv.index('--forge-log') + 1] if '--forge-log' in argv else 'output/s27-forge-test.log'
    out = argv[argv.index('--out') + 1] if '--out' in argv else None
    u = json.load(open(os.path.join(ROOT, 'eval', 'results', 'U-IC2.json')))
    agg = json.load(open(os.path.join(ROOT, 'eval', 'supplements', 'U-IC2-agg.json')))
    rows = {(r['bucket'], r['bot']): r for r in agg['rows'] if r['m'] == 0.022 and not r['observer']}
    txt = open(os.path.join(ROOT, log)).read()
    f6 = bool(re.search(r'\[PASS\] test_F_6__baseAmountIndependentOfSegmentation', txt))
    ok, per = f6, {}
    for b, bk in u['buckets'].items():
        m = rows[(b, MIMIC)]
        e = dict(mimic_net_mean=bk['mimic_mean'], mimic_net_p5=bk['mimic_p5'], mimic_net_before_gas=m['net_mean'] + m['gas_mean'],
                 bots={})
        ok &= bk['mimic_mean'] >= 0
        for bot in BOTS:
            c, r = bk['cmp'][bot], rows[(b, bot)]
            e['bots'][bot] = dict(net_after_gas=r['net_mean'], net_before_gas=r['net_mean'] + r['gas_mean'],
                                  mimic_minus_bot=c['diff'], ci_lo=c['ci_lo'], ok=c['ok'])
            ok &= bool(c['ok']) and c['ci_lo'] > 0
        idle = rows[(b, 'idle')]
        e['idle_net_plus_gas_abs_max'] = idle['net_plus_gas_abs_max']
        ok &= idle['net_plus_gas_abs_max'] == 0
        per[b] = e
    res = {
        'id': 'E4', 'question': 'base-accrual identity and bot ordering',
        'pass_rule': 'mimic net >= 0 and mimic > idle/dust/stale/wide (idle nets 0)',
        'verdict': 'PASS' if ok else 'FAIL',
        'rule_applied': "every bucket: mimic mean net >= 0; U-IC2's paired one-sided 95% lower bound of mimic - bot > 0 "
                        'for idle, dust, stale, wide; idle net + gas exactly 0 on every path (before gas); the F-6 identity '
                        'test passed',
        'data': 'gbm-synthetic + foundry',
        'identity_test': {'name': 'test_F_6__baseAmountIndependentOfSegmentation', 'passed': f6, 'log': log},
        'buckets': per,
        'note': 'idle nets 0 before gas; after gas it loses its daily pokes (about -0.136 ETH per 180 days at 5 gwei)',
        'evidence': {'eval/results/U-IC2.json': sha('eval/results/U-IC2.json'), 'u_ic2_parts': agg['parts'],
                     'eval/e4.py': sha('eval/e4.py')},
        'limits': 'the U-IC2 per-path parts (two files, 670 MB) are not in the repository; their sha256 is recorded and eval/supplements/U-IC2-agg.json holds the aggregates used here; ' + 'synthetic 180-day paths (U-IC2: driftless GBM and the hover path, third-party depth 0, NAV 1000 ETH, '
                  'gas 5 gwei); not replay',
        'commit_public': git('rev-parse', 'HEAD'),
        'commit_internal': git('rev-parse', 'HEAD', cwd=os.path.join(ROOT, 'internal')),
    }
    t = json.dumps(res, indent=1, allow_nan=False) + '\n'
    if out:
        open(os.path.join(ROOT, out), 'w').write(t)
    print(t)
    return 0 if ok else 1


if __name__ == '__main__':
    sys.exit(main(sys.argv[1:]))
