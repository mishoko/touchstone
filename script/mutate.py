#!/usr/bin/env python3
"""Curated mutation runner. Applies ONE textual mutation at a time, runs the named killing test (or the full suite),
records the verdict and restores the file. A surviving mutant has three honest answers: write the missing test,
delete the line, or write down why it cannot be tested. Widening a tolerance is none of them. While a mutant is
applied the runner keeps the original as `<file>.mutorig` and writes `output/MUTATION_IN_PROGRESS`; never run another
`forge test` on this tree meanwhile (`script/markers.py` fails if either is left behind).

  python3 script/mutate.py                  # all mutants
  python3 script/mutate.py MU-003 MU-007    # a subset
  python3 script/mutate.py --full           # the full suite per mutant instead of the named killer
  python3 script/mutate.py --audit          # every // [MU-xxx] tag in src/ has a mutant and every wired mutant
                                            # has its tag in its file
  python3 script/mutate.py --write          # also store each verdict in audits/mutants.json (`status`)

audits/mutants.json: {"mutants": [{"id": "MU-001", "area": "...", "desc": "...", "killer_id": "U-4" | null,
  "kill": "<test function>" | "pending", "file": "src/..." | null, "find": "...", "replace": "...", "status": "..."}]}
Each mutant is tagged in the code it mutates with `// [MU-xxx]` next to the rule.
Verdicts: KILLED (the killer fails on the mutant) | SURVIVED | INVALID (does not compile; not counted as killed) |
BAD-PATTERN (the find string is not unique in the file) | BAD-KILLER (the named killer fails on the clean tree) |
PENDING (no code or no killing test yet). Exit 0 only if the list is non-empty and every selected mutant is KILLED."""
import json, os, re, subprocess, sys, time

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
LIST = os.path.join(ROOT, 'audits', 'mutants.json')
MARK = os.path.join(ROOT, 'output', 'MUTATION_IN_PROGRESS')


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
    pending = sum(1 for m in muts if not wired(m))
    for e in errs:
        print(e)
    print(f'audit: {len(muts)} mutants, {len(muts) - pending} wired, {pending} pending;', 'OK' if not errs else 'FAIL')
    return not errs


def forge(args):
    return subprocess.run(['forge', 'test', '-q'] + args, cwd=ROOT, capture_output=True, text=True,
                          env={**os.environ, 'TS_MUTATION_RUN': '1',
                               'FOUNDRY_PROFILE': os.environ.get('FOUNDRY_PROFILE', 'mutation')})


def compile_error(r):
    out = (r.stdout + r.stderr).lower()
    return 'compiler run failed' in out or 'error (' in out or 'compilation failed' in out


def restore_leftovers():
    """Restores any file left mutated by an interrupted run (its `.mutorig` backup is the original)."""
    for d, _, fs in os.walk(os.path.join(ROOT, 'src')):
        for f in fs:
            if f.endswith('.mutorig'):
                orig = os.path.join(d, f)
                os.replace(orig, orig[:-len('.mutorig')])
                print('restored', orig[:-len('.mutorig')])


FLAGS = ('--audit', '--full', '--write')


def main(argv):
    if any(a in ('-h', '--help') for a in argv):
        print(__doc__)
        return 0
    bad = [a for a in argv if a.startswith('-') and a not in FLAGS]
    if bad:
        # an unknown flag must never fall through to "all mutants" (a campaign that mutates src/)
        print('mutate: unknown option', ' '.join(bad), '; known:', ' '.join(FLAGS))
        return 2
    doc, muts = load()
    if '--audit' in argv:
        return 0 if audit(muts) else 1
    restore_leftovers()
    full = '--full' in argv
    args = [a for a in argv if not a.startswith('--')]
    sel = [m for m in muts if not args or m['id'] in args]
    if not sel:
        print('mutate: no mutant matches', ' '.join(args))
        return 1
    if full and any(wired(m) for m in sel):
        if forge(['--nmp', 'test/fork/*']).returncode != 0:
            print('mutate --full: the clean suite is red; refusing to run')
            return 1
    os.makedirs(os.path.dirname(MARK), exist_ok=True)
    open(MARK, 'w').write(str(os.getpid()))
    res = []
    try:
        clean_ok = {}
        for m in sel:
            if not wired(m):
                res.append((m['id'], 'PENDING', m['desc']))
                continue
            k = m['kill']
            if not full and k not in clean_ok:
                clean_ok[k] = forge(['--mt', k]).returncode == 0
            if not full and not clean_ok[k]:
                res.append((m['id'], 'BAD-KILLER', f"{m['desc']} [{k} fails on the clean tree]"))
                continue
            f = os.path.join(ROOT, m['file'])
            src = open(f).read()
            n = src.count(m['find'])
            if n != 1:
                res.append((m['id'], f'BAD-PATTERN({n})', m['desc']))
                continue
            open(f + '.mutorig', 'w').write(src)
            open(f, 'w').write(src.replace(m['find'], m['replace']))
            try:
                t = time.time()
                r = forge(['--nmp', 'test/fork/*'] if full else ['--mt', k])
                st = 'INVALID' if compile_error(r) else ('KILLED' if r.returncode != 0 else 'SURVIVED')
                res.append((m['id'], st, f"{m['desc']} [{'full' if full else k}] {time.time() - t:.0f}s"))
            finally:
                open(f, 'w').write(src)
                if os.path.exists(f + '.mutorig'):
                    os.remove(f + '.mutorig')
    finally:
        os.remove(MARK)
    for r in res:
        print(' | '.join(r))
    killed = sum(1 for r in res if r[1] == 'KILLED')
    print(f'killed {killed}/{len(res)}')
    if '--write' in argv:
        st = {r[0]: r[1] for r in res}
        for m in muts:
            if m['id'] in st:
                m['status'] = st[m['id']]
        open(LIST, 'w').write(json.dumps(doc, indent=1, ensure_ascii=False) + '\n')
    return 0 if res and killed == len(res) else 1


if __name__ == '__main__':
    sys.exit(main(sys.argv[1:]))
