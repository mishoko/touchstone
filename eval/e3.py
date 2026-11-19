#!/usr/bin/env python3
"""E3 (spec §10): no curator action => zero charge, over 10^4 checkpoint segments.

Drives the independent reference model (`model/reference.py`, derived from the spec text, never from the Solidity)
through the TEST tier: one curator placement, its markout settled, then N segments with no curator action. Each
segment moves the pool price by third-party swaps (a seeded random walk inside the placed range, the reference
following the pool), advances the clock with permissionless reference refreshes, and closes the segment with a
`poke` checkpoint. After the placement's own records are frozen, every later record must carry charge = 0,
bankBurned = 0, bondBurned = 0, and no markout may be pending; the curator's bank may only gain (base) or vest.

  python3 eval/e3.py [--segments 10000] [--seed 1108] [--out eval/results/E3.json]
"""
import argparse, hashlib, json, os, random, subprocess, sys, time

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, 'model'))
import reference as R  # noqa: E402
from selfcheck import World, sqrt_at, T0  # noqa: E402


def sha(p):
    return hashlib.sha256(open(os.path.join(ROOT, p), 'rb').read()).hexdigest()


def git(*a, cwd=ROOT):
    return subprocess.run(['git', *a], cwd=cwd, capture_output=True, text=True).stdout.strip()


def run(n, seed):
    rng = random.Random(seed)
    w = World(third_L=10 ** 19)
    w.ref_tick = lambda ts: w.v.pool.tick  # the reference follows the pool (no fence pressure, no lag)
    half = w.t.W_TICKS // 2
    w.place(T0 - half, T0 + half)
    w.settle_due()
    w.keepalive_run(w.t.GRID)
    w.poke()
    frozen = {e: dict(r) for e, r in w.v.records.items()}
    swaps = 0
    t0 = time.time()
    for i in range(n):
        for _ in range(rng.randint(1, 3)):
            target = T0 + rng.randint(-half + 50, half - 50)
            cur = w.v.pool.tick
            if target != cur:
                w.v.pool.swap(target < cur, 10 ** 30, sqrt_at(target))
                swaps += 1
        w.keepalive_run(rng.randint(1, 2 * w.t.GRID))
        w.poke()
    bad = []
    charge = bank_burn = bond_burn = 0
    for e, r in w.v.records.items():
        base = frozen.get(e, {})
        dc = r['charge'] - base.get('charge', 0)
        db = r['bankBurned'] - base.get('bankBurned', 0)
        dd = r['bondBurned'] - base.get('bondBurned', 0)
        charge, bank_burn, bond_burn = charge + dc, bank_burn + db, bond_burn + dd
        if dc or db or dd:
            bad.append((e, dc, db, dd))
    pending, reserve = w.v.pending is not None, w.v.bank.reserveShares
    # positive control (same harness, after the no-action segments): one curator swap pays the pool fee, so the
    # pool prices a charge > 0 (the harness can see a charge when one exists)
    before = {e: r['charge'] for e, r in w.v.records.items()}
    w.step(w.t.COOLDOWN)
    w.touch()
    w.v.rebalance_swap(True, 1_000 * 10 ** 6, w.block, w.ts())
    w.settle_due()
    control = sum(r['charge'] - before.get(e, 0) for e, r in w.v.records.items())
    return dict(control_charge_wei=control, segments=n, third_party_swaps=swaps, epochs=len(w.v.records), charge_wei=charge,
                bank_burned_shares=bank_burn, bond_burned_shares=bond_burn, pending_markout=pending,
                reserve_shares=reserve, records_with_a_charge=bad[:5],
                minted_base_shares=sum(r['minted'] for r in w.v.records.values()), seconds=round(time.time() - t0, 1))


def main(argv):
    ap = argparse.ArgumentParser()
    ap.add_argument('--segments', type=int, default=10_000)
    ap.add_argument('--seed', type=int, default=1108)
    ap.add_argument('--out', default=None)
    a = ap.parse_args(argv)
    r = run(a.segments, a.seed)
    ok = (r['charge_wei'] == 0 and r['bank_burned_shares'] == 0 and r['bond_burned_shares'] == 0
          and not r['pending_markout'] and r['reserve_shares'] == 0 and r['segments'] >= 10_000
          and r['control_charge_wei'] > 0)
    res = {
        'id': 'E3',
        'question': 'no action => zero charge over 10^4 segments',
        'pass_rule': 'within rounding',
        'verdict': 'PASS' if ok else 'FAIL',
        'data': 'model',
        'data_detail': 'the independent reference model, TEST tier; third-party swaps as a seeded random walk',
        'numbers': r,
        'rule_applied': 'exactly 0: charge, bank burn, bond burn over every no-action segment; no pending markout; '
                        'positive control: one curator swap in the same harness prices a charge > 0',
        'seed': a.seed,
        'evidence': {'model/reference.py': sha('model/reference.py'), 'eval/e3.py': sha('eval/e3.py'),
                     'tests': ['test_F_5__noActionMeasuresZero', 'test_F_5__noActionMeasuresZeroOnTheVault']},
        'limits': 'independent model, not the contracts (F-5 covers the contracts at <= 8 swaps x 64 runs); the '
                  'reference follows the pool, so the fence never binds; not on replay swaps',
        'commit_public': git('rev-parse', 'HEAD'),
        'commit_internal': git('rev-parse', 'HEAD', cwd=os.path.join(ROOT, 'internal')),
    }
    txt = json.dumps(res, indent=1, allow_nan=False) + '\n'
    if a.out:
        open(os.path.join(ROOT, a.out), 'w').write(txt)
    print(txt)
    return 0 if ok else 1


if __name__ == '__main__':
    sys.exit(main(sys.argv[1:]))
