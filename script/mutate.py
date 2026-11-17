#!/usr/bin/env python3
"""Curated mutation runner. Applies ONE textual mutation at a time, runs the named killing test (or the full suite),
records the verdict and restores the file. A surviving mutant has three honest answers: write the missing test,
delete the line, or write down why it cannot be tested. Widening a tolerance is none of them. While a mutant is
applied the runner keeps the original as `<file>.mutorig` and writes `output/MUTATION_IN_PROGRESS`; never run another
`forge test` on this tree meanwhile (`script/markers.py` fails if either is left behind).

  python3 script/mutate.py                  # all mutants
  python3 script/mutate.py MU-003 MU-007    # a subset
  python3 script/mutate.py --changed        # only the mutants whose basis changed since their last KILLED (below)
  python3 script/mutate.py --since <rev>    # the mutants whose file, killer files or linked libraries changed since
                                            # the git revision <rev> (the close rule), plus every --changed one
  python3 script/mutate.py -j 4 ...         # 4 parallel workers, each in its own copy under output/mutation-wt/
  python3 script/mutate.py --full           # the full suite per mutant instead of the named killer
  python3 script/mutate.py --audit          # every // [MU-xxx] tag in src/ has a mutant and every wired mutant
                                            # has its tag in its file
  python3 script/mutate.py --write          # also store each verdict (and a KILLED mutant's basis) in mutants.json

audits/mutants.json: {"mutants": [{"id": "MU-001", "area": "...", "desc": "...", "killer_id": "U-4" | null,
  "kill": "<test name pattern>" | "pending", "file": "src/..." | null, "find": "...", "replace": "...",
  "status": "...", "basis": "<hash>"}]}
Each mutant is tagged in the code it mutates with `// [MU-xxx]` next to the rule.

Verdicts (from `forge test --json`): KILLED (a test matching the killer pattern, or its suite's setUp, fails on the
mutant) | SURVIVED | INVALID (does not compile; not counted as killed) | ERROR (no test matched the killer, or the run
produced no result: never counted as killed) | BAD-PATTERN (the find string is not unique in the file) | BAD-KILLER
(the named killer fails on the clean tree) | PENDING (no code or no killing test yet). Exit 0 only if the list is
non-empty and every selected mutant is KILLED.

A mutant's basis is a hash of what its verdict depends on: the function containing its find string; the files that
define its killer and every test/ or script/ file they import, transitively; `foundry.toml`; the golden vectors when
any of those files reads a file; every file under src/libraries/ and src/types/ (the coarse-linked entries and the
internal libraries inlined into the contracts); and for a cap, charge, solvency or settlement mutant (areas guards,
ledger, bank; in any file) the vault's hot path `_action`, `_charge`, `_settle` and `_cappedSwap`. A change elsewhere
in a contract (another function of the vault) is not in the basis: the close rule (`--since`) re-runs every mutant
of a changed file. Full campaigns still run at each phase close, at the freeze and after any structural
move; the public kill rate comes from one full run at one commit."""
import hashlib, json, os, re, shutil, subprocess, sys, time
from concurrent.futures import ThreadPoolExecutor
from queue import Queue

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
# TS_MUTANTS_LIST points at another list (the serial/parallel equivalence check uses a list with a known no-op)
LIST = os.environ.get('TS_MUTANTS_LIST') or os.path.join(ROOT, 'audits', 'mutants.json')
MARK = os.path.join(ROOT, 'output', 'MUTATION_IN_PROGRESS')
WT = os.path.join(ROOT, 'output', 'mutation-wt')
VAULT = 'src/TouchstoneVault.sol'
HOT = ('_action', '_charge', '_settle', '_cappedSwap')
HOT_AREAS = ('guards', 'ledger', 'bank')


def load():
    doc = json.load(open(LIST)) if os.path.exists(LIST) else {}
    muts = doc.get('mutants', []) if isinstance(doc, dict) else []
    if not muts:
        print('mutate: the mutant list is empty; refusing to report a vacuous pass')
        sys.exit(1)
    return doc, muts


def wired(m):
    return bool(m.get('file') and m.get('find') is not None and m.get('replace') is not None
                and m.get('kill') and m['kill'] != 'pending')


def audit(muts):
    tags = {}
    for d, _, fs in os.walk(os.path.join(ROOT, 'src')):
        for f in fs:
            if f.endswith('.sol'):
                p = os.path.relpath(os.path.join(d, f), ROOT)
                for t in re.findall(r'\[(MU-\d+)\]', open(os.path.join(ROOT, p)).read()):
                    tags.setdefault(t, set()).add(p)
    ids = {m['id']: m for m in muts}
    errs = [f'tag without a mutant: {t}' for t in sorted(set(tags) - set(ids))]
    for m in muts:
        if m.get('file') and m['file'] not in tags.get(m['id'], set()):
            errs.append(f"mutant without its tag in {m['file']}: {m['id']}")
    # every wired mutant's find string matches its file exactly once (a stale or ambiguous pattern is not a kill)
    for m in muts:
        if wired(m):
            n = open(os.path.join(ROOT, m['file'])).read().count(m['find'])
            if n != 1:
                errs.append(f"find string matches {n} times in {m['file']}: {m['id']}")
    # every wired mutant's killer pattern names at least one test (a pattern matching nothing is never a kill)
    names = _test_names()
    for m in muts:
        if wired(m) and not any(re.search(m['kill'], n) for n in names):
            errs.append(f"killer pattern matches no test: {m['id']} {m['kill']}")
    pending = sum(1 for m in muts if not wired(m))
    for e in errs:
        print(e)
    print(f'audit: {len(muts)} mutants, {len(muts) - pending} wired, {pending} pending;', 'OK' if not errs else 'FAIL')
    return not errs


# ------------------------------------------------------------------ the basis (what a verdict depends on)

def _read(p):
    try:
        return open(os.path.join(ROOT, p)).read()
    except OSError:
        return ''


def _test_files():
    out = []
    for d, _, fs in os.walk(os.path.join(ROOT, 'test')):
        out += [os.path.relpath(os.path.join(d, f), ROOT) for f in fs if f.endswith('.sol')]
    return sorted(out)


def _test_names():
    names = set()
    for f in _test_files():
        names |= set(re.findall(r'function (test\w*|invariant\w*)\s*\(', _read(f)))
    return names


def _function_text(src, idx):
    """The function, modifier or constructor enclosing `idx` (the whole file if none does)."""
    starts = [m.start() for m in re.finditer(r'^[ \t]*(function|modifier|constructor)\b', src, re.M) if m.start() <= idx]
    if not starts:
        return src
    s = starts[-1]
    b = src.find('{', s)
    if b < 0 or b > idx and src.find(';', s) < b:
        return src
    depth = 0
    for i in range(b, len(src)):
        depth += {'{': 1, '}': -1}.get(src[i], 0)
        if depth == 0:
            return src[s:i + 1] if i >= idx else src
    return src


def _named_function(src, name):
    m = re.search(r'^[ \t]*function ' + re.escape(name) + r'\s*\(', src, re.M)
    return _function_text(src, src.index('{', m.start())) if m else ''


def _killer_files(kill):
    """The files defining the killer, plus every test/ or script/ file they import, transitively (src/ is covered by
    the function, library and hot-path parts of the basis)."""
    files = [f for f in _test_files() if any(re.search(kill, n)
                                              for n in re.findall(r'function (\w+)\s*\(', _read(f)))]
    deps, todo = set(files), list(files)
    while todo:
        f = todo.pop()
        for imp in re.findall(r'import\s+[^;]*?"(\.[^"]+)"', _read(f)):
            p = os.path.normpath(os.path.join(os.path.dirname(f), imp))
            if (p.startswith('test' + os.sep) or p.startswith('script' + os.sep)) and p not in deps:
                deps.add(p)
                todo.append(p)
    return sorted(deps)


def _tree_files(*dirs):
    out = []
    for d0 in dirs:
        for d, _, fs in os.walk(os.path.join(ROOT, d0)):
            out += [os.path.relpath(os.path.join(d, f), ROOT) for f in fs if f.endswith('.sol')]
    return sorted(out)


def basis_parts(m):
    """(the files the basis reads, the hash). Files are repo-relative."""
    h = hashlib.sha256()
    src = _read(m['file'])
    i = src.find(m['find'])
    h.update(b'fn:' + _function_text(src, i if i >= 0 else 0).encode())
    files = [m['file']]
    kf = _killer_files(m['kill'])
    for f in kf:
        h.update(f'killer:{f}:'.encode() + _read(f).encode())
    files += kf
    extra = ['foundry.toml']
    if any('readFile' in _read(f) for f in kf):
        extra += sorted(os.path.relpath(os.path.join(ROOT, 'test', 'vectors', v), ROOT)
                        for v in os.listdir(os.path.join(ROOT, 'test', 'vectors')) if v.endswith('.json'))
    extra += _tree_files('src/libraries', 'src/types')
    for f in extra:
        h.update(f'dep:{f}:'.encode() + _read(f).encode())
    files += extra
    if m.get('area') in HOT_AREAS:
        vs = _read(VAULT)
        for n in HOT:
            h.update(f'hot:{n}:'.encode() + _named_function(vs, n).encode())
    return files, h.hexdigest()[:16]


# ------------------------------------------------------------------ running one mutant

def forge(args, cwd=ROOT):
    return subprocess.run(['forge', 'test', '--json'] + args, cwd=cwd, capture_output=True, text=True,
                          env={**os.environ, 'TS_MUTATION_RUN': '1',
                               'FOUNDRY_PROFILE': os.environ.get('FOUNDRY_PROFILE', 'mutation')})


def compile_error(r):
    out = (r.stdout + r.stderr).lower()
    return 'compiler run failed' in out or 'error (' in out or 'compilation failed' in out


def verdict(r, kill, full):
    """KILLED / SURVIVED / INVALID / ERROR from a `forge test --json` run."""
    if compile_error(r):
        return 'INVALID'
    raw = r.stdout
    try:
        res = json.loads(raw[raw.index('{'):])
    except ValueError:
        return 'ERROR'
    matched, failed = 0, 0
    for _, suite in res.items():
        tr = suite.get('test_results', {})
        setup_failed = any(n.startswith('setUp') and t.get('status') == 'Failure' for n, t in tr.items())
        for n, t in tr.items():
            if full or re.search(kill, n) or (setup_failed and n.startswith('setUp')):
                matched += 1
                failed += t.get('status') == 'Failure'
    if matched == 0:
        return 'ERROR'
    return 'KILLED' if failed else 'SURVIVED'


def restore_leftovers(root=ROOT):
    """Restores any file left mutated by an interrupted run (its `.mutorig` backup is the original)."""
    for d, _, fs in os.walk(os.path.join(root, 'src')):
        for f in fs:
            if f.endswith('.mutorig'):
                orig = os.path.join(d, f)
                os.replace(orig, orig[:-len('.mutorig')])
                print('restored', orig[:-len('.mutorig')])


def run_one(m, root, full, clean_ok):
    k = m['kill']
    if not full and not clean_ok(k, root):
        return (m['id'], 'BAD-KILLER', f"{m['desc']} [{k} fails on the clean tree]")
    f = os.path.join(root, m['file'])
    src = open(f).read()
    n = src.count(m['find'])
    if n != 1:
        return (m['id'], f'BAD-PATTERN({n})', m['desc'])
    open(f + '.mutorig', 'w').write(src)
    open(f, 'w').write(src.replace(m['find'], m['replace']))
    try:
        t = time.time()
        r = forge(['--nmp', 'test/fork/*'] if full else ['--mt', k], cwd=root)
        return (m['id'], verdict(r, k, full), f"{m['desc']} [{'full' if full else k}] {time.time() - t:.0f}s")
    finally:
        open(f, 'w').write(src)
        if os.path.exists(f + '.mutorig'):
            os.remove(f + '.mutorig')


# ------------------------------------------------------------------ parallel workers

COPY_EXCLUDE = {'.git', 'out', 'cache', 'output', 'lib', 'internal', 'node_modules', 'broadcast', 'out-oversized',
                'cache-oversized'}


def make_worker(i):
    """A private copy of the tree (src, test, script, config; lib symlinked; out and cache cloned warm)."""
    w = os.path.join(WT, f'w{i}')
    if os.path.isdir(w):
        shutil.rmtree(w)
    os.makedirs(w)
    for e in os.listdir(ROOT):
        if e in COPY_EXCLUDE or e.startswith('.') and e not in ('.gitmodules',):
            continue
        s = os.path.join(ROOT, e)
        (shutil.copytree if os.path.isdir(s) else shutil.copy2)(s, os.path.join(w, e))
    os.symlink(os.path.join(ROOT, 'lib'), os.path.join(w, 'lib'))
    for d in ('out', 'cache'):
        if os.path.isdir(os.path.join(ROOT, d)):
            # APFS clone (cp -c) keeps the copy cheap; fall back to a plain copy
            if subprocess.run(['cp', '-cR', os.path.join(ROOT, d), os.path.join(w, d)]).returncode != 0:
                shutil.copytree(os.path.join(ROOT, d), os.path.join(w, d), dirs_exist_ok=True)
    os.makedirs(os.path.join(w, 'output'), exist_ok=True)
    return w


# ------------------------------------------------------------------ selection

def git(*a):
    return subprocess.run(['git'] + list(a), cwd=ROOT, capture_output=True, text=True)


def select_changed(muts):
    return [m for m in muts if wired(m) and m.get('basis') != basis_parts(m)[1]]


def select_since(muts, rev):
    r = git('diff', '--name-only', rev)
    if r.returncode != 0:
        print('mutate --since:', r.stderr.strip())
        sys.exit(2)
    changed = set(r.stdout.split()) | set(git('ls-files', '--others', '--exclude-standard').stdout.split())
    sel = []
    for m in muts:
        if wired(m) and (set(basis_parts(m)[0]) & changed or m.get('basis') != basis_parts(m)[1]):
            sel.append(m)
    return sel


FLAGS = ('--audit', '--full', '--write', '--changed')


def main(argv):
    if any(a in ('-h', '--help') for a in argv):
        print(__doc__)
        return 0
    jobs, since, rest, i = 1, None, [], 0
    while i < len(argv):
        a = argv[i]
        if a == '-j' and i + 1 < len(argv):
            jobs, i = int(argv[i + 1]), i + 2
            continue
        if a == '--since' and i + 1 < len(argv):
            since, i = argv[i + 1], i + 2
            continue
        rest.append(a)
        i += 1
    argv = rest
    bad = [a for a in argv if a.startswith('-') and a not in FLAGS]
    if bad:
        # an unknown flag must never fall through to "all mutants" (a campaign that mutates src/)
        print('mutate: unknown option', ' '.join(bad), '; known:', ' '.join(FLAGS), '-j N --since REV')
        return 2
    doc, muts = load()
    if '--audit' in argv:
        return 0 if audit(muts) else 1
    if os.path.exists(MARK):
        try:
            pid = int(open(MARK).read().strip() or 0)
            os.kill(pid, 0)
            print(f'mutate: another campaign (pid {pid}) is running on this tree; refusing to start')
            return 1
        except (ValueError, ProcessLookupError, PermissionError):
            print('mutate: removing a stale', MARK)
    restore_leftovers()
    full = '--full' in argv
    args = [a for a in argv if not a.startswith('--')]
    if since:
        sel = select_since(muts, since)
    elif '--changed' in argv:
        sel = select_changed(muts)
    else:
        sel = [m for m in muts if not args or m['id'] in args]
    if args and (since or '--changed' in argv):
        sel = [m for m in sel if m['id'] in args]
    if not sel:
        print('mutate: no mutant selected' + (' (no basis changed)' if since or '--changed' in argv else ''))
        return 0 if (since or '--changed' in argv) else 1
    print(f'mutate: {len(sel)} mutants selected, {jobs} worker(s)')
    if full and any(wired(m) for m in sel):
        if forge(['--nmp', 'test/fork/*']).returncode != 0:
            print('mutate --full: the clean suite is red; refusing to run')
            return 1
    os.makedirs(os.path.dirname(MARK), exist_ok=True)
    open(MARK, 'w').write(str(os.getpid()))
    res = []
    # every killer passes on the clean tree first, checked once in the (unmutated, warm) main tree before any mutation
    cache = {}
    todo = [m for m in sel if wired(m)]
    if not full:
        for k in sorted({m['kill'] for m in todo}):
            cache[k] = verdict(forge(['--mt', k]), k, False) == 'SURVIVED'

    def clean_ok(k, root):
        return cache[k]

    try:
        res += [(m['id'], 'PENDING', m['desc']) for m in sel if not wired(m)]
        if jobs <= 1:
            for m in todo:
                res.append(run_one(m, ROOT, full, clean_ok))
                print(' | '.join(res[-1]), flush=True)
        else:
            roots = Queue()
            probe = todo[0]['kill']
            for w in range(jobs):
                root = make_worker(w)
                # the copy must pass a clean killer before it may judge a mutant (a missing file would read as KILLED)
                if not full and verdict(forge(['--mt', probe], cwd=root), probe, False) != 'SURVIVED':
                    print(f'mutate: worker copy {root} fails the clean killer {probe}; refusing to run')
                    return 1
                roots.put(root)

            def task(m):
                root = roots.get()
                try:
                    r = run_one(m, root, full, clean_ok)
                    print(' | '.join(r), flush=True)
                    return r
                finally:
                    roots.put(root)

            ex = ThreadPoolExecutor(max_workers=jobs)
            try:
                res += list(ex.map(task, todo))
            except KeyboardInterrupt:
                ex.shutdown(wait=True, cancel_futures=True)
                raise
            ex.shutdown(wait=True)
    finally:
        os.remove(MARK)
        if os.path.isdir(WT):
            for w in os.listdir(WT):
                restore_leftovers(os.path.join(WT, w))
    print('---')
    for r in res:
        print(' | '.join(r))
    killed = sum(1 for r in res if r[1] == 'KILLED')
    print(f'killed {killed}/{len(res)}')
    if '--write' in argv:
        st = {r[0]: r[1] for r in res}
        for m in muts:
            if m['id'] in st:
                m['status'] = st[m['id']]
                if st[m['id']] == 'KILLED':
                    m['basis'] = basis_parts(m)[1]
                else:
                    m.pop('basis', None)
        open(LIST, 'w').write(json.dumps(doc, indent=1, ensure_ascii=False) + '\n')
    return 0 if res and killed == len(res) else 1


if __name__ == '__main__':
    sys.exit(main(sys.argv[1:]))
