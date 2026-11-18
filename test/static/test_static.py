#!/usr/bin/env python3
"""Static checks of the tree. Run: python3 test/static/test_static.py (also collected by pytest)."""
import os, re, subprocess, sys, tempfile, time

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
ANVIL_PK = '0xac0974bec39a17e36ba4a6b4d238ff944bacb478cbed5efcae784d7bf4f2ff80'  # anvil's default account 0


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


def fork_bypasses(root):
    """Solidity test files that select a fork without the chain-checked `ForkBase._fork` (F-052)."""
    bad = []
    for d, _, fs in os.walk(os.path.join(root, 'test')):
        for f in fs:
            p = os.path.join(d, f)
            if f.endswith('.sol') and f != 'ForkBase.sol' and re.search(r'\b(createSelectFork|createFork|selectFork)\s*\(',
                                                                          open(p).read()):
                bad.append(os.path.relpath(p, root))
    return bad


def test_S_5__every_fork_is_chain_checked():
    assert fork_bypasses(ROOT) == [], fork_bypasses(ROOT)
    # control: a test file that forks directly is caught
    with tempfile.TemporaryDirectory() as d:
        os.makedirs(os.path.join(d, 'test', 'fork'))
        open(os.path.join(d, 'test', 'fork', 'X.t.sol'), 'w').write('vm.createSelectFork(url);')
        assert fork_bypasses(d) == ['test/fork/X.t.sol']


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
        'claim': 'curator', 'topUpBond': 'curator', 'retire': 'curator', 'releaseBond': 'curator', 'poke': 'anyone',
        'processBreach': 'anyone', 'roll': 'vault-delegatecall',
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


def test_S_2__hard_size_gate_with_an_anvil_deploy_and_the_oversized_fixture_failing():
    """S-2 (spec §4.12 gates, §9.1): every src/ runtime <= 22,000 B by `forge build --sizes`, a real anvil deploy of
    every src/ contract with each deployed runtime read back <= 22,000 B, and the gate rejecting the oversized fixture."""
    r = run('script/size_gate.py')
    assert r.returncode == 0 and 'size gate OK' in r.stdout, r.stdout + r.stderr
    # control: the oversized fixture fails the same gate, by its size
    r = run('script/size_gate.py', '--scope', 'test/fixtures', env={'FOUNDRY_PROFILE': 'oversized'})
    assert r.returncode == 1 and re.search(r'Oversized: runtime \d+ B > 22000 B', r.stdout), r.stdout + r.stderr
    port = os.environ.get('ANVIL_PORT', '8617')
    rpc = f'http://127.0.0.1:{port}'
    a = subprocess.Popen(['anvil', '--port', port, '--silent'], cwd=ROOT)
    try:
        for _ in range(50):
            if subprocess.run(['cast', 'chain-id', '--rpc-url', rpc], capture_output=True).returncode == 0:
                break
            time.sleep(0.2)
        d = subprocess.run(['forge', 'script', 'script/AnvilDeploy.s.sol', '--rpc-url', rpc, '--broadcast',
                            '--private-key', ANVIL_PK], cwd=ROOT, capture_output=True, text=True)
        assert d.returncode == 0, d.stdout[-2000:] + d.stderr[-2000:]
        c = run('script/anvil_check.py', '--rpc-url', rpc)
        assert c.returncode == 0, c.stdout + c.stderr
    finally:
        a.terminate()
        a.wait()


if __name__ == '__main__':
    tests = [v for k, v in sorted(globals().items()) if k.startswith('test_') and callable(v)]
    for t in tests:
        t()
        print('PASS', t.__name__)
    print(f'{len(tests)} static tests passed')
