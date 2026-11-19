#!/usr/bin/env python3
"""E7 (spec §10): gas and size. Pass: budgets.

Sizes from `forge build --sizes --json src` on the current tree (limit 22,000 B, EIP-170 with margin); gas from the
committed snapshots (`snapshots/*.json`, rewritten by every `forge test`); the reference-read gas G-1b from the chain-1
fork record (block 26443420, s22: `eval/supplements/E7/fork-pin-26443420.json`; `src/` has had comment-only edits
since). Budgets (spec §9.1): G-1 <= 30k, G-1b <= REFRESH_GAS_MIN + 20k (REFRESH_GAS_MIN = 260,000·64/63 + 30,000 on
the mainnet tier), G-2 <= 6k, G-3 <= 700k, G-4 <= 1.2M.

  python3 eval/e7.py [--out eval/results/E7.json]
"""
import hashlib, json, os, subprocess, sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, 'script'))
import size_gate  # noqa: E402

REFRESH_GAS_MIN = 260_000 * 64 // 63 + 30_000


def sha(p):
    return hashlib.sha256(open(os.path.join(ROOT, p), 'rb').read()).hexdigest()


def git(*a, cwd=ROOT):
    return subprocess.run(['git', *a], cwd=cwd, capture_output=True, text=True).stdout.strip()


def main(argv):
    out = argv[argv.index('--out') + 1] if '--out' in argv else None
    r = subprocess.run(['forge', 'build', '--sizes', '--json', 'src'], cwd=ROOT, capture_output=True, text=True, check=True)
    rep = json.loads(r.stdout)
    names = size_gate.declared('src')
    sizes = {n: rep[n]['runtime_size'] for n in sorted(names) if n in rep}
    errs = size_gate.gate(rep, names, size_gate.LIMIT)
    hook = json.load(open(os.path.join(ROOT, 'snapshots', 'TouchstoneHook.json')))
    vault = json.load(open(os.path.join(ROOT, 'snapshots', 'TouchstoneVault.json')))
    fork = json.load(open(os.path.join(ROOT, 'eval', 'supplements', 'E7', 'fork-pin-26443420.json')))['G1b']
    g = {k: int(v) for k, v in {**hook, **vault}.items()}
    checks = {
        'G-1 first swap per block': (g['G-1 first swap per block'], 30_000),
        'G-1 grid-boundary block without a read': (g['G-1 grid-boundary block without a read'], 30_000),
        'G-1b refresh block, fork, full ring 65,535': (fork['full_ring_65535']['refresh_block'], REFRESH_GAS_MIN + 20_000),
        'G-1b refresh block, fork, live cardinality 723': (fork['live_card_723']['refresh_block'], REFRESH_GAS_MIN + 20_000),
        'G-2 later swap in the block': (g['G-2 later swap in the block'], 6_000),
        'G-3 poke (3 breaches, freeze, settle, gap roll, witness)': (g['G-3 poke (3 breaches, freeze, settle, gap roll, witness)'], 700_000),
        'G-3 poke (vest, mint, settle, gap roll, witness)': (g['G-3 poke (vest, mint, settle, gap roll, witness)'], 700_000),
        'G-4 setPositions worst case': (g['G-4 setPositions worst case'], 1_200_000),
        'G-4 rebalanceSwap worst case': (g['G-4 rebalanceSwap worst case'], 1_200_000),
    }
    gas = {k: dict(gas=v, budget=b, ok=v <= b) for k, (v, b) in checks.items()}
    ok = not errs and all(x['ok'] for x in gas.values())
    res = {
        'id': 'E7', 'question': 'gas and size', 'pass_rule': 'budgets', 'verdict': 'PASS' if ok else 'FAIL',
        'data': 'foundry (local) and fork (chain 1, block 26443420, for G-1b)',
        'sizes_runtime_bytes': sizes, 'size_limit_bytes': size_gate.LIMIT, 'size_errors': errs, 'gas': gas,
        'evidence': {'snapshots/TouchstoneHook.json': sha('snapshots/TouchstoneHook.json'),
                     'snapshots/TouchstoneVault.json': sha('snapshots/TouchstoneVault.json'),
                     'eval/supplements/E7/fork-pin-26443420.json': sha('eval/supplements/E7/fork-pin-26443420.json')},
        'limits': 'G-1b was measured on the fork in s22; src/ has changed since only in comments (mutant tags) and is '
                  'byte-identical in runtime apart from the metadata hash; the anvil deploy check runs in ci-local',
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
