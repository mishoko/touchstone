#!/usr/bin/env python3
"""Static checks of the tree. Run: python3 test/static/test_static.py (also collected by pytest)."""
import glob, json, os, re, subprocess, sys, tempfile, time

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
    """Places that select a fork without the chain-checked `test/fork/ForkBase.sol` `_fork` (F-052): a Solidity test
    calling a fork cheatcode, or a config or script setting a fork URL (`eth_rpc_url`, `fork_url`, `--fork-url`)."""
    bad = []
    exempt = os.path.join('test', 'fork', 'ForkBase.sol')
    for d, _, fs in os.walk(os.path.join(root, 'test')):
        for f in fs:
            p = os.path.join(d, f)
            rel = os.path.relpath(p, root)
            if f.endswith('.sol') and rel != exempt and re.search(r'\b(createSelectFork|createFork|selectFork)\s*\(',
                                                                    open(p).read()):
                bad.append(rel)
    cfg = [os.path.join(root, 'foundry.toml')] + [q for e in ('sh', 'py') for q in
                                                   glob.glob(os.path.join(root, 'script', '**', '*.' + e), recursive=True)]
    for p in cfg:
        t = open(p).read() if os.path.exists(p) else ''
        if re.search(r'(?i)eth_rpc_url|fork_url|--fork-url', t) or \
                re.search(r'''forge["',\s]+test[^\n]*(--rpc-url|\s-f\b|["']-f["'])''', t):
            bad.append(os.path.relpath(p, root))
    return bad


def test_S_5__every_fork_is_chain_checked():
    assert fork_bypasses(ROOT) == [], fork_bypasses(ROOT)
    # controls: a test file that forks directly, a ForkBase.sol outside test/fork, and a fork URL in the config
    with tempfile.TemporaryDirectory() as d:
        os.makedirs(os.path.join(d, 'test', 'fork'))
        os.makedirs(os.path.join(d, 'test', 'unit'))
        os.makedirs(os.path.join(d, 'script'))
        open(os.path.join(d, 'test', 'fork', 'X.t.sol'), 'w').write('vm.createSelectFork(url);')
        open(os.path.join(d, 'test', 'unit', 'ForkBase.sol'), 'w').write('vm.createFork(url);')
        open(os.path.join(d, 'foundry.toml'), 'w').write('[profile.fork]\neth_rpc_url = "x"\n')
        open(os.path.join(d, 'script', 'run.sh'), 'w').write('forge test --fork-url $X\n')
        os.makedirs(os.path.join(d, 'script', 'demo'))
        open(os.path.join(d, 'script', 'demo', 'go.py'), 'w').write("run('forge test --rpc-url ' + u)\n")
        open(os.path.join(d, 'script', 'f.sh'), 'w').write('forge test -f "$X"\n')
        open(os.path.join(d, 'script', 'env.sh'), 'w').write('FOUNDRY_ETH_RPC_URL=$X forge test\n')
        open(os.path.join(d, 'script', 'list.py'), 'w').write('run(["forge", "test", "--rpc-url", u])\n')
        assert sorted(fork_bypasses(d)) == ['foundry.toml', 'script/demo/go.py', 'script/env.sh', 'script/f.sh',
                                            'script/list.py', 'script/run.sh', 'test/fork/X.t.sol',
                                            'test/unit/ForkBase.sol'], fork_bypasses(d)


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


SLITHER_SECTIONS = {'High': ('High',), 'Medium': ('Medium',), 'Low': ('Low',),
                    'Informational and optimisation': ('Informational', 'Optimization')}


def slither_triage_violations(md, rec, inputs_hash):
    """S-1 (F-053): `audits/SLITHER.md` triages exactly the recorded Slither run `audits/slither-results.json`, made from
    the current inputs. Checked: the record's input hash; the triage's `Record: sha256` (any change to the record forces
    a re-triage); the stated total and tool; per `## ` section (every heading is a boundary; unknown headings fail): the
    header count, the detector set and each row's count equal the record's, no detector in two rows, a verdict (FP,
    Design or Accepted) on every row; the header counts sum to the total; on High rows, the flagged element of every
    result is named in the row's Where cell."""
    sys.path.insert(0, os.path.join(ROOT, 'script'))
    from slither_record import results_sha256
    bad = []
    if rec.get('input_sha256') != inputs_hash:
        bad.append('the recorded run is not of the current inputs (re-run Slither, script/slither_record.py, re-triage)')
    if not re.search(r'Record: sha256 `?' + results_sha256(rec['results']) + r'\b', md):
        bad.append('the triage does not name this record (Record: sha256 ...): re-triage after regenerating it')
    counts, first = {}, {}
    for r in rec['results']:
        counts.setdefault(r['impact'], {}).setdefault(r['check'], 0)
        counts[r['impact']][r['check']] += 1
        names = [e['name'] for e in r['elements'] if e['type'] != 'node']
        first.setdefault(r['check'], []).append(names[0] if names else None)
    m = re.search(r'Result: (\d+) results', md)
    if not m or int(m.group(1)) != len(rec['results']):
        bad.append(f"stated total {m and m.group(1)} != {len(rec['results'])} recorded results")
    if rec.get('tool', '').split()[-1:] != (re.findall(r'Tool: Slither ([\d.]+)', md) or ['?'])[:1]:
        bad.append(f"tool {rec.get('tool')} differs from the triage's")
    heads = list(re.finditer(r'^## (.+?)\s*$', md, re.M))
    if heads and re.search(r'^\|[^\n]*`[a-z0-9-]+`', md[:heads[0].start()], re.M):
        bad.append('a detector row before the first section')
    seen, rows_of, total = set(), {}, 0
    for i, h in enumerate(heads):
        hm = re.fullmatch(r'(.+?) \((\d+)\)', h.group(1))
        if not hm or hm.group(1) not in SLITHER_SECTIONS:
            bad.append(f'unknown section {h.group(1)}')
            continue
        name, n = hm.group(1), int(hm.group(2))
        seen.add(name)
        total += n
        rc = {}
        for imp in SLITHER_SECTIONS[name]:
            rc.update(counts.get(imp, {}))
        body = md[h.end():heads[i + 1].start() if i + 1 < len(heads) else len(md)]
        if n != sum(rc.values()):
            bad.append(f'{name}: header {n} != {sum(rc.values())} recorded')
        triaged = set()
        for row in re.findall(r'^\|(.+)\|\s*$', body, re.M):
            cells = [c.strip() for c in row.split('|')]
            dets = re.findall(r'`([a-z0-9-]+)`', cells[0])
            if not dets:
                continue   # the header and separator rows
            for x in dets:
                rows_of[x] = rows_of.get(x, 0) + 1
            triaged |= set(dets)
            if not cells[1].isdigit() or int(cells[1]) != sum(rc.get(x, 0) for x in dets):
                bad.append(f'{name}: row {dets} count {cells[1]} != {sum(rc.get(x, 0) for x in dets)} recorded')
            if not re.match(r'\*\*(FP|Design|Accepted)\b', cells[-1]):
                bad.append(f'{name}: row {dets} has no verdict')
            if name == 'High':
                rec_names = {el for x in dets for el in first.get(x, [])}
                for w in re.findall(r'`([^`(]+)`', cells[2]):       # element references, not code expressions
                    w = w.split('.')[-1]
                    if re.fullmatch(r'\w+', w) and w not in rec_names:
                        bad.append(f'High: row {dets}: Where names {w}, which the record does not flag')
                for x in dets:
                    for el in first.get(x, []):
                        if el is None or not re.search(r'\b' + re.escape(el) + r'\b', cells[2]):
                            bad.append(f'High: row {dets}: element {el} not named in its Where cell')
        if triaged != set(rc):
            bad.append(f'{name}: untriaged {sorted(set(rc) - triaged)}, not in the run {sorted(triaged - set(rc))}')
    bad += [f'detector {x} in {k} rows' for x, k in rows_of.items() if k > 1]
    if m and total != int(m.group(1)):
        bad.append(f'section headers sum to {total}, not the stated total {m.group(1)}')
    for name in set(SLITHER_SECTIONS) - seen:
        if any(counts.get(imp) for imp in SLITHER_SECTIONS[name]):
            bad.append(f'section {name} missing')
    return bad


def test_S_1__slither_triage_covers_the_recorded_run_of_the_current_src():
    sys.path.insert(0, os.path.join(ROOT, 'script'))
    from slither_record import input_sha256
    md = open(os.path.join(ROOT, 'audits', 'SLITHER.md')).read()
    rec = json.load(open(os.path.join(ROOT, 'audits', 'slither-results.json')))
    h = input_sha256(ROOT)
    assert slither_triage_violations(md, rec, h) == [], slither_triage_violations(md, rec, h)
    # controls: one per violation branch; each must mutate its input and fail with its own message
    det = rec['results'][0]['check']                       # a High detector (the record is sorted by impact name)
    assert rec['results'][0]['impact'] == 'High'
    extra = dict(rec, results=rec['results'] + [dict(check='new-detector', impact='High', confidence='High', elements=[])])
    swapped = json.loads(json.dumps(rec))                   # same counts, a different flagged element
    el = next(e for e in swapped['results'][0]['elements'] if e['type'] != 'node')
    el['name'] = 'totallyDifferent'
    from slither_record import results_sha256
    md_sw = md.replace(results_sha256(rec['results']), results_sha256(swapped['results']))   # even when re-hashed
    dropped = json.loads(json.dumps(rec))                   # a named element no longer flagged (same counts)
    e2 = next(e for e in dropped['results'][0]['elements'] if e['type'] != 'node')
    e2['name'] = next(e['name'] for e in dropped['results'][1]['elements'] if e['type'] != 'node')
    md_dr = md.replace(results_sha256(rec['results']), results_sha256(dropped['results']))
    low_det = next(r['check'] for r in rec['results'] if r['impact'] == 'Low')
    lowrow = re.search(r'^\|[^\n]*`' + re.escape(low_det) + r'`[^\n]*\n', md, re.M).group(0)
    moved = md.replace(lowrow, '').replace('## Low (', '## Low (X').replace('## Medium (', '## Medium (Y')
    moved = re.sub(r'(## Medium \(Y[^\n]*\n(?:[^\n]*\n){3})', lambda q: q.group(1) + lowrow, moved, count=1)
    moved = moved.replace('(X', '(').replace('(Y', '(')
    cases = {
        'inputs changed': ((md, rec, h[::-1]), 'not of the current inputs'),
        'record regenerated': ((md, swapped, h), 'does not name this record'),
        'element swapped': ((md_sw, swapped, h), 'not named in its Where cell'),
        'row count changed': ((re.sub(r'(`' + re.escape(det) + r'`[^|]*\|\s*)(\d+)', lambda q: q.group(1) +
                                      str(int(q.group(2)) + 1), md, count=1), rec, h), "row ['" + det),
        'header changed': ((md.replace('## High (', '## High (1', 1), rec, h), 'High: header'),
        'untriaged detector': ((md, extra, h), "untriaged ['new-detector']"),
        'verdict missing': ((re.sub(r'^(\|[^\n]*\|\s*)\*\*(FP|Design|Accepted)\b', r'\1maybe', md, count=1, flags=re.M),
                             rec, h), 'has no verdict'),
        'total changed': ((re.sub(r'Result: (\d+)', lambda q: f'Result: {int(q.group(1)) + 1}', md), rec, h),
                          'stated total'),
        'row dropped': ((re.sub(r'^\|[^\n]*`' + re.escape(det) + r'`[^\n]*\n', '', md, count=1, flags=re.M), rec, h),
                        'untriaged [' + repr(det)),
        'duplicate row': ((md.replace(lowrow, lowrow + lowrow), rec, h), f'detector {low_det} in 2 rows'),
        'stray section': ((md + '\n## Notes\n' + lowrow, rec, h), 'unknown section Notes'),
        'section dropped': ((md[:md.index('## Low (')] + md[md.index('## Informational'):], rec, h), 'section Low missing'),
        'detector moved': ((moved, rec, h), 'not in the run [' + repr(low_det)),
        'preamble row': ((md.replace('## High (', lowrow + '## High (', 1), rec, h), 'before the first section'),
        'named element dropped': ((md_dr, dropped, h), 'which the record does not flag'),
        'tool changed': ((md.replace('Tool: Slither ', 'Tool: Slither 9', 1), rec, h), 'differs from the triage'),
    }
    for k, ((m2, r2, h2), want) in cases.items():
        assert m2 != md or r2 is not rec or h2 != h, k
        v = slither_triage_violations(m2, r2, h2)
        assert any(want in x for x in v), (k, v)


STRICT_JSON_EXEMPT = {'U-IC.json'}   # written once before the rule (a bisection Infinity); never rewritten (S23U-03)


def test_eval_results_parse_strictly_and_the_uic_supplement_is_complete():
    """Every eval result parses as strict JSON (no NaN/Infinity) except the named exemption; the U-IC observer
    supplement covers every gated bot, bucket and simulated m; the README names the observer figure (s23 S23U-01/03)."""
    def strict(t):
        return json.loads(t, parse_constant=lambda c: (_ for _ in ()).throw(ValueError(c)))
    for f in glob.glob(os.path.join(ROOT, 'eval', '**', '*.json'), recursive=True):
        if os.path.basename(f) not in STRICT_JSON_EXEMPT:
            strict(open(f).read())
    try:   # control: a bare Infinity is rejected
        strict('{"x": Infinity}')
        raise AssertionError('Infinity accepted')
    except ValueError:
        pass
    sys.path.insert(0, os.path.join(ROOT, 'script'))
    import uic_observer
    assert uic_observer.check() == [], uic_observer.check()
    with tempfile.NamedTemporaryFile('w', suffix='.json', delete=False) as t:   # control: a dropped row is caught
        r = json.load(open(uic_observer.OUT))
        r['comparisons']['gbm-120'].pop('dodge')
        json.dump(r, t)
    try:
        assert uic_observer.check(t.name) == [f'gbm-120 dodge {m:g}' for m in uic_observer.MS]
    finally:
        os.remove(t.name)
    readme = open(os.path.join(ROOT, 'README.md')).read()
    assert 'U-IC-observer.json' in readme and 'outside observer' in readme
    assert 'exactly the minimum deployment' not in readme   # dodge places the smallest legal deployment >= DELTA_MIN
    # the README's figures are the files' (S23U2-03): observer gbm-120 margins at 2.2% and the observer gate minimum
    sup = json.load(open(uic_observer.OUT))
    g = sup['comparisons']['gbm-120']
    for bot in ('dodge', 'breach-reset'):
        assert f"{-g[bot]['0.022']['diff']:.2f} " in readme or f"{-g[bot]['0.022']['diff']:.2f} ETH" in readme, bot
    uic = json.loads(open(os.path.join(ROOT, 'eval', 'results', 'U-IC.json')).read())
    assert f"about {uic['l3_form']['gbm-120']['observer_m_need'] * 100:.1f}%" in readme
    assert f"{uic['l3_form']['gbm-120']['m_need_max'] * 100:.2f}% a year" in readme
    # the supplement's hover note matches its rows (S23U2-01)
    hv = sup['comparisons']['hover-60']
    assert all(v[m]['ok'] for v in hv.values() for m in ('0.01', '0.022')) and 'nonzero m' in sup['note']


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


TIER_FIELDS = ('REF_WINDOW', 'GRID_SEC', 'CURE_BLOCKS', 'EPOCH', 'GRID', 'COOLDOWN', 'FORFEIT_DRIP_BLOCKS', 'H_MARKOUT',
               'EDGE_DELAY')


def tier_literals(src):
    """Per tier function of `script/Tiers.sol`, its numeric `t.<FIELD> = <literal>;` assignments and blocktime."""
    out = {}
    for name, body in re.findall(r'function (mainnet|demo|testing)\(\)[^{]*\{(.*?)\n    \}', src, re.S):
        d = {f: int(v.replace('_', '')) for f, v in re.findall(r'\bt\.([A-Z_]+) = ([0-9_]+);', body)}
        bt = re.search(r'blocktime = ([0-9]+);', body)
        if bt:
            d['BLOCKTIME'] = int(bt.group(1))
        out[name] = d
    return out


def tier_mismatches(src, ref):
    pairs = (('mainnet', ref.MAINNET), ('demo', ref.DEMO), ('testing', ref.TEST))
    lit = tier_literals(src)
    bad = []
    for name, tr in pairs:
        for f in TIER_FIELDS + ('BLOCKTIME',):
            if f not in lit.get(name, {}):
                bad.append(f'{name}.{f} missing in Tiers.sol')
            elif lit[name][f] != getattr(tr, f):
                bad.append(f'{name}.{f}: Tiers.sol {lit[name][f]} != reference {getattr(tr, f)}')
    return bad


def test_tiers_literals_match_the_independent_reference_model():
    """The deployment constants and the reference model's tiers agree (s24 design review S24U-03: the model kept
    mainnet EDGE_DELAY 1800 after Tiers.sol moved to 3600)."""
    sys.path.insert(0, os.path.join(ROOT, 'model'))
    import reference as ref
    src = open(os.path.join(ROOT, 'script', 'Tiers.sol')).read()
    bad = tier_mismatches(src, ref)
    assert not bad, bad
    # control: a changed literal is caught
    assert tier_mismatches(src.replace('t.EDGE_DELAY = 3600;', 't.EDGE_DELAY = 1800;'), ref), 'control'


def test_U_IC2__observer_supplement_is_complete():
    """The U-IC2 observer supplement (both mimics, every gated bot but lazy, bucket and simulated m) is complete; it is
    the reported item the harness table lacked (s24); control: a dropped row is caught."""
    sys.path.insert(0, os.path.join(ROOT, 'script'))
    import uic2_observer
    assert uic2_observer.check() == [], uic2_observer.check()
    with tempfile.NamedTemporaryFile('w', suffix='.json', delete=False) as t:
        r = json.load(open(uic2_observer.OUT))
        r['comparisons']['edge-12h']['gbm-120'].pop('dodge')
        json.dump(r, t)
    try:
        assert uic2_observer.check(t.name) == [('edge-12h', 'gbm-120', 'dodge', m) for m in uic2_observer.MS]
    finally:
        os.remove(t.name)
    # the README's U-IC2 figures are the files' (s24; the s23 rule: result text from the data); line wraps ignored
    readme = ' '.join(open(os.path.join(ROOT, 'README.md')).read().split())
    r = json.load(open(os.path.join(ROOT, 'eval', 'results', 'U-IC2.json')))
    assert r['verdict'] == 'PASS' and 'U-IC2' in readme
    g = r['buckets']['gbm-120']['cmp']
    for bot in ('breach-reset', 'dodge'):
        assert f"+{g[bot]['diff']:.3f} ETH" in readme and f"+{g[bot]['ci_lo']:.3f} ETH" in readme, bot
    lv = r['lever_control']['gbm-120']
    assert f"+{lv['diff']:.3f}" in readme and f"+{lv['ci_lo']:.3f} ETH" in readme
    sys.path.insert(0, os.path.join(ROOT, 'model', 'cells'))
    import bots as B
    lazy_lo = min(t['cmp'][b]['ci_lo'] for t in r['buckets'].values() for b in B.LAZY_FAMILY if b in t['cmp'])
    assert f"+{lazy_lo:.2f} ETH" in readme
    o = json.load(open(uic2_observer.OUT))['comparisons']['edge-12h']['gbm-120']
    assert f"{-o['breach-reset']['0.022']['diff']:.3f} and {-o['dodge']['0.022']['diff']:.3f} ETH" in readme
    nv = r['reported']['nav_end']['gbm-120']['no-observer']
    assert f"about {nv['breach-reset']['mean']:.0f} and {nv['dodge']['mean']:.0f} against {nv['edge-12h']['mean']:.0f}" in readme
    e6 = r['reported']['edge6h_form']['buckets']['gbm-120']['cmp']
    for bot in ('breach-reset', 'dodge'):   # both D4 rows (S24F-03)
        x = e6[bot]
        assert f"+{x['diff']:.3f} ETH, lower bound {x['ci_lo']:+.3f} ETH".replace('-', '−').replace('bound +', 'bound +') \
            in readme, bot
    # S24F-01: the bold headline carries the scope (D5); control: a stripped headline fails
    def scoped(text):
        h = re.search(r'\*\*Curator incentives \(([^)]*)\)', text)
        return bool(h) and 'without a third-party refresher of the reference' in h.group(1)
    assert scoped(readme)
    assert not scoped(readme.replace('measured without a third-party refresher of the reference;', 'measured;'))
    # S24F-02: the hover power control's figure; S24F-04: the NAV limit's scope
    c0 = r['buckets']['hover-60']['control_m0']['diff_lazy_minus_mimic']
    assert f"{c0:.3f} ETH; reported, not gated".replace('-', '−') in readme
    assert 'in every volatility bucket (at 120%: about' in readme and 'not on the hovering path), because' in readme
    # S24F-05: the supplement's disclosure and the frozen artefact as disclosed
    assert 'computed after the run from the same per-run rows' in readme
    assert 'harness table holds only lazy-under-ladder' in json.load(open(uic2_observer.OUT))['why']
    assert all(len(v) <= 1 for v in r['reported']['observer_environment']['edge-12h'].values())


VAULT_FILES = ('src/TouchstoneVault.sol', 'src/TouchstoneVaultBase.sol', 'src/TouchstoneVaultExtension.sol')


CALL = re.compile(r'FullMath\.mulDiv(RoundingUp)?\(')
# lines whose rounding is not mutated, each with its reason (S24R4-01)
ROUNDING_ALLOWLIST = {
    'if (FullMath.mulDiv(navRef, S_SEED - DEAD, S_SEED) < BOND_MIN) revert BondMin();':
        'the seed bond floor: rounding up would accept a bond at most 1 wei below BOND_MIN (a parameter floor, not an '
        'accounting identity); its comparison and price are mutated by MU-130 and MU-161',
}


def flipped_calls(find, replace):
    """Indices of the `mulDiv` calls in `find` whose rounding the mutant flips (same call count, name changed)."""
    a = [m.group(1) or '' for m in CALL.finditer(find)]
    b = [m.group(1) or '' for m in CALL.finditer(replace)]
    return [i for i in range(len(a)) if len(a) == len(b) and a[i] != b[i]]


def uncovered_roundings(srcs, muts):
    """Every `FullMath.mulDiv*` call of the vault's three files has a wired mutant that flips exactly its rounding
    (s24 S24M-01, S24R2-01, S24R3-01, S24R4-01: one arithmetic site per call, each with its own flip; a mutant that
    changes something else on the line does not count). `srcs` maps a repo path to its text."""
    bad = []
    for f, src in srcs.items():
        wired = [m for m in muts if m.get('file') == f and m.get('find') and m.get('kill') not in (None, 'pending')]
        for n, line in enumerate(src.split('\n'), 1):
            k = len(CALL.findall(line))
            if not k or line.strip() in ROUNDING_ALLOWLIST:
                continue
            covered = set()
            for m in wired:
                fd = m['find']
                if line.strip() not in fd:
                    continue
                off = len(CALL.findall(fd[:fd.index(line.strip())]))
                covered |= {i - off for i in flipped_calls(fd, m['replace']) if 0 <= i - off < k}
            if covered != set(range(k)):
                bad.append(f'{f}:{n}: {line.strip()} (flipped calls {sorted(covered)} of {k})')
    return bad


def test_every_vault_rounding_has_a_mutant():
    srcs = {f: open(os.path.join(ROOT, f)).read() for f in VAULT_FILES}
    muts = json.load(open(os.path.join(ROOT, 'audits', 'mutants.json')))['mutants']
    assert uncovered_roundings(srcs, muts) == [], uncovered_roundings(srcs, muts)
    # controls: dropping one of a line's two flips, a single-call line's flip, or the re-base flip (whose line other
    # mutants also touch, S24R4-01) exposes the line
    for mid, needle in (('MU-315', 'M_PER_BLOCK'), ('MU-313', 'o1 = FullMath.mulDiv'), ('MU-317', 'rebased = FullMath')):
        cut = [dict(m, kill='pending') if m['id'] == mid else m for m in muts]
        assert any(needle in b for b in uncovered_roundings(srcs, cut)), mid
    # control: two mutants flipping the same call of a two-call line leave the other call uncovered
    dup = [dict(m, replace=next(x['replace'] for x in muts if x['id'] == 'MU-314')) if m['id'] == 'MU-315' else m
           for m in muts]
    assert any('M_PER_BLOCK' in b for b in uncovered_roundings(srcs, dup))


if __name__ == '__main__':
    tests = [v for k, v in sorted(globals().items()) if k.startswith('test_') and callable(v)]
    for t in tests:
        t()
        print('PASS', t.__name__)
    print(f'{len(tests)} static tests passed')
