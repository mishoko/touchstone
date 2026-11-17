#!/usr/bin/env python3
"""Static checks of the tree. Run: python3 test/static/test_static.py (also collected by pytest)."""
import os, subprocess, sys, tempfile

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


def run(*args, env=None):
    return subprocess.run([sys.executable, *args], cwd=ROOT, capture_output=True, text=True,
                          env={**os.environ, **(env or {})})


def test_S_3__claims_due_by_the_current_phase_are_covered():
    phase = os.environ.get('CLAIMS_PHASE', 'P0')
    r = run('script/claims.py', env={'CLAIMS_PHASE': phase})
    assert r.returncode == 0, r.stdout + r.stderr
    assert run('script/claims.py', '--selftest').returncode == 0
    # control: an uncovered ID due now fails the check
    with tempfile.NamedTemporaryFile('w', suffix='.md', delete=False) as f:
        f.write('| ID | Due | Kind |\n|---|---|---|\n| S-3 | P0 | test |\n| X-uncovered | P0 | test |\n')
    try:
        r = run('script/claims.py', '--claims', f.name, env={'CLAIMS_PHASE': 'P0'})
        assert r.returncode == 1 and 'X-uncovered' in r.stdout, r.stdout
    finally:
        os.remove(f.name)


def test_S_4__no_mutation_markers_in_the_tree():
    r = run('script/markers.py')
    assert r.returncode == 0, r.stdout
    # control: a leftover backup or in-progress marker fails the check
    for name in ('src/X.sol.mutorig', 'output/MUTATION_IN_PROGRESS'):
        with tempfile.TemporaryDirectory() as d:
            p = os.path.join(d, name)
            os.makedirs(os.path.dirname(p), exist_ok=True)
            open(p, 'w').write('')
            assert run('script/markers.py', '--root', d).returncode == 1, name


# Every state-changing entry point of the hook, the vault (with its extension's allow-listed entries) and the factory,
# with the only caller class that passes its guard. None is an owner or admin: D-3's Solidity half
# (test/deploy/Privilege.t.sol) shows each guard refuses the deployer and the factory after seed.
D3_ENTRIES = {
    'TouchstoneHook': {
        'ackBreaches': 'vault', 'armAtSeed': 'vault', 'fundWitness': 'vault', 'rollWitness': 'vault',
        'setVaultRange': 'vault', 'witnessStep': 'self', 'refreshRef': 'anyone', 'snapshotIfNewBlock': 'anyone',
        'beforeInitialize': 'poolManager', 'afterInitialize': 'poolManager', 'beforeAddLiquidity': 'poolManager',
        'afterAddLiquidity': 'poolManager', 'beforeRemoveLiquidity': 'poolManager',
        'afterRemoveLiquidity': 'poolManager', 'beforeSwap': 'poolManager', 'afterSwap': 'poolManager',
        'beforeDonate': 'poolManager', 'afterDonate': 'poolManager',
    },
    'TouchstoneVault': {
        'setPositions': 'curator', 'rebalanceSwap': 'curator', 'settle': 'anyone', 'unlockCallback': 'poolManager',
        'approve': 'holder', 'transfer': 'holder', 'transferFrom': 'holder', '<fallback>': 'allow-list',
    },
    'TouchstoneVaultExtension': {
        'seed': 'factory-once', 'deposit': 'anyone', 'withdraw': 'anyone', 'dispatch': 'vault-delegatecall',
        'approve': 'holder', 'transfer': 'holder', 'transferFrom': 'holder',
    },
    'TouchstoneFactory': {'deploy': 'anyone'},
}
D3_FORBIDDEN = ('owner', 'admin', 'pause', 'upgrade', 'rescue', 'sweep', 'setparam', 'settier', 'setfee', 'setcurator',
                'setref', 'kill', 'destruct', 'migrate', 'grant', 'revoke')


def d3_entries(out_dir):
    import json
    found = {}
    for c in D3_ENTRIES:
        abi = json.load(open(os.path.join(out_dir, c + '.sol', c + '.json')))['abi']
        names = {x['name'] for x in abi if x['type'] == 'function' and x.get('stateMutability') not in ('view', 'pure')}
        names |= {'<fallback>' for x in abi if x['type'] in ('fallback', 'receive')}
        found[c] = names
    return found


def d3_violations(found):
    bad = []
    for c, names in found.items():
        extra = names - set(D3_ENTRIES[c])
        missing = set(D3_ENTRIES[c]) - names
        if extra:
            bad.append(f'{c}: entry points outside the table: {sorted(extra)}')
        if missing:
            bad.append(f'{c}: table entries not in the ABI: {sorted(missing)}')
        bad += [f'{c}.{n}: privileged name' for n in names if any(w in n.lower() for w in D3_FORBIDDEN)]
    return bad


def test_D_3__abi_has_no_privileged_entry_point():
    out = os.path.join(ROOT, 'out')
    if not os.path.exists(os.path.join(out, 'TouchstoneHook.sol')):
        raise AssertionError('run forge build first (D-3 reads the ABIs in out/)')
    import json
    for c in D3_ENTRIES:
        art = os.path.join(out, c + '.sol', c + '.json')
        srcs = json.load(open(art))['metadata']['sources']
        newest = max(os.path.getmtime(os.path.join(ROOT, f)) for f in srcs if os.path.exists(os.path.join(ROOT, f)))
        assert os.path.getmtime(art) >= newest, f'{art} is older than one of its sources: run forge build first'
    bad = d3_violations(d3_entries(out))
    assert not bad, '\n'.join(bad)
    # controls: an added owner setter and an unlisted entry point both fail the check
    planted = d3_entries(out)
    planted['TouchstoneVault'] = planted['TouchstoneVault'] | {'setOwner'}
    planted['TouchstoneFactory'] = planted['TouchstoneFactory'] | {'deploy2'}
    bad = d3_violations(planted)
    assert any('setOwner' in b for b in bad) and any('deploy2' in b for b in bad), bad


if __name__ == '__main__':
    tests = [v for k, v in sorted(globals().items()) if k.startswith('test_') and callable(v)]
    for t in tests:
        t()
        print('PASS', t.__name__)
    print(f'{len(tests)} static tests passed')
