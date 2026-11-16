#!/usr/bin/env python3
"""Checks an anvil deploy of `script/AnvilDeploy.s.sol`: every concrete contract under src/ was created, and each
created src/ contract has a deployed runtime of 1..22,000 B (read back with `cast codesize`).

  python3 script/anvil_check.py --rpc-url http://127.0.0.1:8545
"""
import json, os, re, subprocess, sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
LIMIT = 22_000
RUN = os.path.join(ROOT, 'broadcast', 'AnvilDeploy.s.sol', '31337', 'run-latest.json')


def src_contracts():
    names = set()
    for d, _, fs in os.walk(os.path.join(ROOT, 'src')):
        for f in fs:
            if f.endswith('.sol'):
                s = re.sub(r'//[^\n]*|/\*.*?\*/', '', open(os.path.join(d, f)).read(), flags=re.S)
                names |= set(re.findall(r'^\s*contract\s+([A-Za-z_]\w*)', s, re.M))
    return names


def main(argv):
    rpc = argv[argv.index('--rpc-url') + 1] if '--rpc-url' in argv else 'http://127.0.0.1:8545'
    txs = json.load(open(RUN))['transactions']
    created = {t['contractName']: t['contractAddress'] for t in txs if t.get('transactionType') == 'CREATE'}
    errs = [f'{c}: not deployed' for c in sorted(src_contracts() - set(created))]
    for c in sorted(src_contracts() & set(created)):
        r = subprocess.run(['cast', 'codesize', created[c], '--rpc-url', rpc], capture_output=True, text=True)
        if r.returncode != 0:
            errs.append(f'{c}: cast codesize failed: {r.stderr.strip()}')
            continue
        n = int(r.stdout.strip())
        print(f'  {c} @ {created[c]}: {n} B')
        if not 0 < n <= LIMIT:
            errs.append(f'{c}: deployed runtime {n} B outside 1..{LIMIT}')
    for e in errs:
        print('anvil deploy FAIL:', e)
    if not errs:
        print('anvil deploy OK')
    return 1 if errs else 0


if __name__ == '__main__':
    sys.exit(main(sys.argv[1:]))
