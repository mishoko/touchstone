#!/usr/bin/env python3
"""E6 (spec §10): the churner. Pass: t < -3 (follows from construction; labelled so).

Pre-registered in the s27 P6 plan (design review S27P6D-07), fixed before any E6 data existed:
- churner = the shipped mimic (edge-12h: re-places after 12 h out, keep-alive, epoch pokes) that ALSO, at every
  hourly mark where the mimic did not act, re-places between two distinct legal ranges (containing spot and the
  reference, W wide, aligned, delta_after >= DELTA_MIN), alternating; COOLDOWN (30 blocks) is below the hourly
  decision grid, so the hourly mark is the churner's cadence;
- m = 2.2%/yr, no observer, buckets gbm-40, gbm-60, gbm-120 and hover-60 (`incentives.KINDS`), N = 200 seeds per
  bucket (1308-1507, never used by any run; their RNG keys are asserted disjoint from those of seeds 0-1307);
- paired on the same path and Sim seed: d = net(churner) - net(mimic); t = mean(d) / (sd(d)/sqrt(N)); PASS iff
  t < -3 in every bucket;
- non-vacuity: on every path the churner executes actions beyond the mimic's and pays charges > 0, else FAIL.
The churner is defined here, not in `model/` (the model hash, and so U-IC2's reproduction, is unchanged).

  python3 eval/e6.py [procs] [--out eval/results/E6.json]
  python3 eval/e6.py --time    # timing only: one spent seed, prints the seconds, no result
"""
import hashlib, json, math, multiprocessing as mp, os, subprocess, sys, time

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
CELLS = os.path.join(ROOT, 'model', 'cells')
sys.path.insert(0, CELLS)
import sim as S  # noqa: E402
import bots as B  # noqa: E402
import incentives as I  # noqa: E402
import regate_l3 as L3  # noqa: E402
from common import R, T0  # noqa: E402

FIRST, N = 1308, 200
M = 0.022
MIMIC = 'edge-12h'
WAD = 10 ** 18


class Churner(B.Edge60):
    name = 'churner'

    def __init__(self):
        super().__init__(delay_h=12, name='churner')
        self.extra = 0

    def post(self, sim):
        a0 = sim.counts['actions']
        super().post(sim)
        if sim.counts['actions'] != a0 or not B.can_act(sim):
            return
        ref = sim.live_ref()
        if ref is None:
            return
        cur = sim.v.hook.vaultLo
        cands = [lo for lo in dict.fromkeys((B.best_delta_range(sim, ref, 'max'), B.best_delta_range(sim, ref, 'min')))
                 if lo is not None and lo != cur]
        if not cands:
            for lo in (cur + B.SPACING, cur - B.SPACING):
                if B._legal(lo, sim.v.pool.tick, ref, sim.t.REF_BAND) and B._delta_at(
                        B._inventory_after_removal(sim), lo, lo + B.W, sim.v.pool.s, B.sqrt_at(ref)) >= B.DMIN:
                    cands = [lo]
                    break
        if not cands:
            return
        ok, _ = sim.vcall('set_positions', cands[0], cands[0] + B.W, R.Vault.NO_PRESWAP, gas='place')
        if ok:
            sim.counts['actions'] += 1
            self.extra += 1


def keys(seed):
    out = set()
    for kind, sigma in I.KINDS:
        out.add(('path', S.path_key(seed, sigma, kind)))
        out.add(('sim', L3.sim_key(seed, sigma)))
    return out


def job(args):
    seed, kind, sigma = args
    S.SETTLE['rule'], S.SETTLE['int_closes'] = 'fee_aware', True
    path = S.market_path(seed, sigma, I.DAYS, kind, edge=T0 + 1000)
    out = {}
    for name in (MIMIC, 'churner'):
        sim = S.Sim(path, M, observer=False, place=True, sigma=sigma, rng_seed=L3.sim_key(seed, sigma), bond_ratio=WAD)
        bot = Churner() if name == 'churner' else B.make(MIMIC)
        r = sim.run(bot, I.DAYS * 24)
        out[name] = dict(net=r['net'], charges=r['charges'], gas=r['gas'], actions=r['actions'],
                         reverts=sum(r['revert_kinds'].values()) if isinstance(r.get('revert_kinds'), dict) else None,
                         extra=getattr(bot, 'extra', 0))
    return dict(seed=seed, kind=kind, sigma=sigma, **out)


def sha(p):
    return hashlib.sha256(open(os.path.join(ROOT, p), 'rb').read()).hexdigest()


def model_hash():
    h = hashlib.sha256()
    for d in ('model', os.path.join('model', 'cells')):
        for f in sorted(os.listdir(os.path.join(ROOT, d))):
            if f.endswith('.py'):
                h.update(f.encode() + open(os.path.join(ROOT, d, f), 'rb').read())
    return h.hexdigest()


def git(*a, cwd=ROOT):
    return subprocess.run(['git', *a], cwd=cwd, capture_output=True, text=True).stdout.strip()


def main(argv):
    if argv[:1] == ['--time']:
        t = time.time()
        job((1, 'gbm', 1.20))
        print(f'one gbm-120 path (mimic + churner): {time.time() - t:.1f} s')
        return 0
    procs = int(argv[0]) if argv and argv[0].isdigit() else 4
    out_path = argv[argv.index('--out') + 1] if '--out' in argv else None
    fresh = set().union(*(keys(s) for s in range(FIRST, FIRST + N)))
    spent = set().union(*(keys(s) for s in range(0, FIRST)))
    assert not fresh & spent, 'E6 seeds share an RNG key with seeds 0-1307'
    jobs = [(sd, k, s) for (k, s) in I.KINDS for sd in range(FIRST, FIRST + N)]
    t0 = time.time()
    with mp.Pool(procs) as p:
        rows = p.map(job, jobs, chunksize=4)
    buckets, ok, nonvac = {}, True, True
    for kind, sigma in I.KINDS:
        b = f'{kind}-{int(sigma * 100)}'
        g = [r for r in rows if r['kind'] == kind and r['sigma'] == sigma]
        d = [r['churner']['net'] - r[MIMIC]['net'] for r in g]
        n = len(d)
        mu = sum(d) / n
        sd = math.sqrt(sum((x - mu) ** 2 for x in d) / (n - 1))
        t = mu / (sd / math.sqrt(n)) if sd > 0 else float('-inf') if mu < 0 else 0.0
        nv = all(r['churner']['extra'] > 0 and r['churner']['charges'] > 0 for r in g)
        nonvac &= nv
        ok &= n == N and t < -3
        buckets[b] = dict(n=n, mean_diff_eth=round(mu, 6), sd_eth=round(sd, 6), t=round(t, 3) if math.isfinite(t) else None,
                          churner_net_mean=round(sum(r['churner']['net'] for r in g) / n, 6),
                          mimic_net_mean=round(sum(r[MIMIC]['net'] for r in g) / n, 6),
                          churner_actions_mean=round(sum(r['churner']['actions'] for r in g) / n, 1),
                          mimic_actions_mean=round(sum(r[MIMIC]['actions'] for r in g) / n, 1),
                          churner_charges_mean=round(sum(r['churner']['charges'] for r in g) / n, 6),
                          churner_gas_mean=round(sum(r['churner']['gas'] for r in g) / n, 6),
                          churner_reverts_mean=round(sum((r['churner']['reverts'] or 0) for r in g) / n, 1),
                          nonvacuous=nv)
    res = {
        'id': 'E6', 'question': 'churner', 'pass_rule': 't < -3 (follows from construction; labelled so)',
        'verdict': 'PASS' if ok and nonvac else 'FAIL',
        'label': 'follows from construction: every action is charged at least its measured cost and pays gas, and '
                 're-placing earns no extra base',
        'data': 'gbm-synthetic', 'seeds': [FIRST, FIRST + N - 1], 'm': M, 'mimic': MIMIC, 'observer': False,
        'buckets': buckets, 'nonvacuous': nonvac, 'wall_s': round(time.time() - t0, 1),
        'model_hash': model_hash(), 'evidence': {'eval/e6.py': sha('eval/e6.py')},
        'limits': 'synthetic 180-day paths (driftless GBM and the hover path), third-party depth 0, NAV 1000 ETH, '
                  'gas 5 gwei; the churner acts at hourly marks (the simulation grid), not every COOLDOWN; not replay',
        'commit_public': git('rev-parse', 'HEAD'),
        'commit_internal': git('rev-parse', 'HEAD', cwd=os.path.join(ROOT, 'internal')),
    }
    txt = json.dumps(res, indent=1, allow_nan=False) + '\n'
    if out_path:
        open(os.path.join(ROOT, out_path), 'w').write(txt)
    print(txt)
    return 0 if res['verdict'] == 'PASS' else 1


if __name__ == '__main__':
    sys.exit(main(sys.argv[1:]))
