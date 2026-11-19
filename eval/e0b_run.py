#!/usr/bin/env python3
"""E0b's run wrapper: the only way to produce E0b data (the form's §3.2 "Freeze").

  python3 eval/e0b_run.py seed:<n> <outdir>        # a generated script (n >= 1100 only at the registration commit)
  python3 eval/e0b_run.py fit:<same|offset|exit> <outdir>
  python3 eval/e0b_run.py closure                  # print the frozen closure C with sha256s

Before and after its `forge test` it asserts a clean tree (`git status --porcelain --untracked-files=all`, `output/`
excepted) and the recorded `git submodule status --recursive`; it clears every `FOUNDRY_*` variable and refuses if a
`.env` exists in the tree's root; it writes `<outdir>/manifest-<tag>.json` with the commit, both statuses, the
submodules, the sha256 of every file of the harness closure H, the seed or fit name, the script's sha256, the run name,
`forge --version`, the TS_* environment and the sha256 of every data file it wrote. A `fit:` run also checks the fit
data against the pinned E0a files (truth byte-identical; logs identical up to the vault's address, `VAULT_RELABEL`).

C = the Solidity import closure of `test/eval/E0bData.t.sol` (through `remappings.txt`) + `foundry.toml`,
`remappings.txt` + `eval/e0b_gen.py`, `eval/e0b_run.py`, `eval/E0b-form.md` (= H) + `eval/E0b.py`, `eval/E0a.py`.
"""
import hashlib, json, os, re, subprocess, sys

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, HERE)
import e0b_gen  # noqa: E402

HARNESS = 'test/eval/E0bData.t.sol'
H_EXTRA = ('foundry.toml', 'remappings.txt', 'eval/e0b_gen.py', 'eval/e0b_run.py', 'eval/E0b-form.md',
           'eval/E0a-data.sha256')
EVAL = ('eval/E0b.py', 'eval/E0a.py')
# E0a's six data files (s21), pinned by sha256; the fit runs must reproduce them
E0A_PIN_FILE = os.path.join(HERE, 'E0a-data.sha256')   # one "<sha256>  <name>" line per file (written once, s25)
# the vault deploys at another CREATE2 address in E0b's harness: its creation code's CBOR metadata hash covers the
# source comments changed in src/ since E0a's run; the address appears only in `Transfer` topics
VAULT_RELABEL = ('0x00000000000000000000000054c91d80c98984603b65a694042d58d35e172aac',
                 '0x00000000000000000000000070d1ad3db7a1d742e1f09255a7bbec2d074e01af')


def sh(cmd, **kw):
    return subprocess.run(cmd, cwd=ROOT, capture_output=True, text=True, **kw)


def sha(path):
    h = hashlib.sha256()
    with open(path, 'rb') as f:
        for b in iter(lambda: f.read(1 << 20), b''):
            h.update(b)
    return h.hexdigest()


def remaps():
    out = []
    for line in open(os.path.join(ROOT, 'remappings.txt')):
        line = line.strip()
        if '=' in line:
            a, b = line.split('=', 1)
            out.append((a, b))
    return sorted(out, key=lambda x: -len(x[0]))


IMPORT = re.compile(r'^\s*import\s+(?:[^"\']*from\s+)?["\']([^"\']+)["\']', re.M)


def closure():
    """The Solidity import closure of the harness, as repo-relative paths."""
    rm = remaps()
    seen, todo = set(), [HARNESS]
    while todo:
        p = todo.pop()
        if p in seen:
            continue
        seen.add(p)
        src = open(os.path.join(ROOT, p)).read()
        for imp in IMPORT.findall(src):
            if imp.startswith('.'):
                q = os.path.normpath(os.path.join(os.path.dirname(p), imp))
            else:
                q = next((b + imp[len(a):] for a, b in rm if imp.startswith(a)), imp)
                q = os.path.normpath(q)
            if not os.path.isfile(os.path.join(ROOT, q)):
                raise SystemExit(f'e0b_run: unresolved import {imp} in {p}')
            todo.append(q)
    return sorted(seen)


def harness_hashes():
    """sha256 of every file of H (the harness closure), from the working tree."""
    return {p: sha(os.path.join(ROOT, p)) for p in closure() + [x for x in H_EXTRA if os.path.exists(os.path.join(ROOT, x))]}


def eval_hashes():
    return {p: sha(os.path.join(ROOT, p)) for p in EVAL if os.path.exists(os.path.join(ROOT, p))}


def submodules():
    return sh(['git', 'submodule', 'status', '--recursive']).stdout.strip()


def status():
    return sh(['git', 'status', '--porcelain', '--untracked-files=all', '--', '.', ':!output']).stdout.strip()


def head():
    return sh(['git', 'rev-parse', 'HEAD']).stdout.strip()


def fit_matches_e0a(outdir, run):
    """(ok, why): the fit run's truth byte-identical to E0a's pinned file; its logs identical up to VAULT_RELABEL."""
    pins = dict(l.split()[::-1] for l in open(E0A_PIN_FILE) if l.strip())
    for kind in ('truth', 'logs'):
        mine = os.path.join(outdir, f'E0b-fit-{run}-{kind}.jsonl')
        if kind == 'truth':
            if sha(mine) != pins[f'E0a-{run}-truth.jsonl']:
                return False, f'{run} truth differs from E0a'
        else:
            relabeled = open(mine).read().replace(VAULT_RELABEL[1], VAULT_RELABEL[0])
            if hashlib.sha256(relabeled.encode()).hexdigest() != pins[f'E0a-{run}-logs.jsonl']:
                return False, f'{run} logs differ from E0a beyond the vault address'
    return True, 'ok'


def main(argv):
    if len(argv) >= 2 and argv[1] == 'closure':
        for p, h in sorted({**harness_hashes(), **eval_hashes()}.items()):
            print(h, p)
        return 0
    if len(argv) != 3 or not re.match(r'^(seed:\d+|fit:(same|offset|exit))$', argv[1]):
        print(__doc__)
        return 2
    what, outdir = argv[1], os.path.abspath(argv[2])
    if not outdir.startswith(os.path.join(ROOT, 'output') + os.sep):
        print('e0b_run: the outdir must be under output/')
        return 2
    if os.path.exists(os.path.join(ROOT, '.env')):
        print('e0b_run: a .env exists in the tree root; refused')
        return 1
    st0, sub0 = status(), submodules()
    if st0:
        print('e0b_run: the tree is not clean (output/ excepted); refused\n' + st0)
        return 1
    os.makedirs(outdir, exist_ok=True)
    env = {k: v for k, v in os.environ.items() if not k.startswith('FOUNDRY_') and not k.startswith('TS_')}
    env['TS_E0B_OUT'] = os.path.relpath(outdir, ROOT)
    man = dict(what=what, commit=head(), forge=sh(['forge', '--version']).stdout.strip(), submodules=sub0,
               status_before=st0)
    if what.startswith('seed:'):
        n = int(what.split(':')[1])
        if n >= e0b_gen.SPENT_BELOW:
            ok, why = e0b_gen.registration_ok(head())
            if not ok:
                print(f'e0b_run: seed {n} is held out; refused ({why})')
                return 1
        tag = f'e0b-{n}'
        try:
            script = json.dumps(e0b_gen.generate(n))
        except SystemExit as ex:                    # a script the generator cannot produce: a FAILED script
            man.update(seed=n, run_name=tag, passed=False, outcome='generation_failed', reason=str(ex),
                       status_after=status(), harness=harness_hashes(), data={})
            json.dump(man, open(os.path.join(outdir, f'manifest-{tag}.json'), 'w'), indent=1)
            print(f'e0b_run: {what} FAIL (generation failed); manifest-{tag}.json written')
            return 1
        spath = os.path.join(outdir, f'script-{n}.json')
        open(spath, 'w').write(script)
        env.update(TS_E0B_SCRIPT=os.path.relpath(spath, ROOT), TS_E0B_RUN=tag)
        mt = 'test_E0bData__script'
        man.update(seed=n, run_name=tag, script_sha256=hashlib.sha256(script.encode()).hexdigest())
    else:
        name = what.split(':')[1]
        tag = f'fit-{name}'
        mt = f'test_E0bData__fit_{name}'
        man.update(fit=name, run_name=name)
    man['ts_env'] = {k: v for k, v in env.items() if k.startswith('TS_')}
    # outcomes (S25C2-01): pass; script_failed (forge ran and the script reverted) and timeout (over 1 h) are FAILED
    # scripts; interrupted (forge killed by a signal) is refused by the evaluator (the re-run route of form 6)
    try:
        r = subprocess.run(['forge', 'test', '--mc', 'E0bDataTest', '--mt', mt], cwd=ROOT, env=env,
                           capture_output=True, text=True, timeout=3600)
        rc, out = r.returncode, r.stdout
        outcome = 'pass' if rc == 0 and '[PASS]' in out else ('interrupted' if rc < 0 else 'script_failed')
    except subprocess.TimeoutExpired as ex:
        rc, out, outcome = None, (ex.stdout or b'').decode(errors='replace') if isinstance(ex.stdout, bytes) else (ex.stdout or ''), 'timeout'
    passed = outcome == 'pass'
    man.update(returncode=rc, outcome=outcome)
    st1, sub1 = status(), submodules()
    if st1 or sub1 != sub0:
        print('e0b_run: the tree or the submodules changed during the run; the data are void')
        return 1
    files = sorted(f for f in os.listdir(outdir) if f.startswith(f'E0b-{tag}-'))
    if os.path.exists(os.path.join(outdir, 'E0b-tier.json')):
        files.append('E0b-tier.json')
    man.update(status_after=st1, passed=passed, harness=harness_hashes(),
               data={f: sha(os.path.join(outdir, f)) for f in files})
    if what.startswith('fit:'):
        ok, why = fit_matches_e0a(outdir, man['fit'])
        man['matches_e0a'] = why
        if not ok:
            passed = False
    man['passed'] = passed
    json.dump(man, open(os.path.join(outdir, f'manifest-{tag}.json'), 'w'), indent=1)
    print(f'e0b_run: {what} {"PASS" if passed else "FAIL"}; manifest-{tag}.json written')
    if not passed:
        print(out[-3000:])
    return 0 if passed else 1


if __name__ == '__main__':
    sys.exit(main(sys.argv))
