#!/usr/bin/env python3
"""E0b's held-out script generator (spec §10 "E0b frozen form"; the form's §3.2, `eval/E0b-form.md`).

  python3 eval/e0b_gen.py <seed> [--registered <sha>]      # prints the script JSON on stdout

Deterministic in the seed (one `random.Random(seed)` stream; the numbered draw list of the form, cited as (1)…(10) in
the code). Seeds 0–1099 are spent; a seed >= 1100 is refused unless the registration guard `registration_ok` passes
(the first `registered: true` commit on `e0b`, its parent the reviewed candidate, HEAD equal to it, a clean tree).

The script is a flat op list of width 6, `[code, epoch, offset, a, b, c]`, executed by
`test/eval/E0bData.t.sol:test_E0bData__script`:
  0 WALK  to ep(k)+offset at intensity a (0 refresh only; 1 E0a's 20–30-tick steps; 3 steps of 60–90)
  1 POKE    2 SETTLE    3 PLACE at offset a from the live witness    4 STALE on    5 STALE off
  6 CROSS   a = edge (0 lower, 1 upper), b = depth past it, c = hold blocks with no touch, then the return
  7 REBAL   a = bps of NAV                 8 UNOBSERVED span of a blocks
  9 FLOW    a = 0 deposit / 1 withdraw, b = deposit bps of S, c = withdraw percent of bob's shares
          (a withdraw with no shares held becomes a deposit of b)
  10 CLAIM
The walk's target is the fixed tick 100 ± 80 by epoch parity (E0a's `offset` pattern: an oscillation around a fixed
tick, no ratchet), applied by the harness.
"""
import json, os, random, subprocess, sys

EPOCH, COOLDOWN, END, SUB = 120, 14, 110, 20
OFFSETS = (-600, -300, 0, 300, 600)
EPOCHS = 12
SPENT_BELOW = 1100
HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
FROZEN = os.path.join(HERE, 'E0b-frozen.json')
W, POKE, SETTLE, PLACE, STALE_ON, STALE_OFF, CROSS, REBAL, UNOBS, FLOW, CLAIM = range(11)
VAULT_OPS = (POKE, SETTLE, PLACE, REBAL, FLOW, CLAIM)


# ---------------------------------------------------------------- the registration guard (shared with E0b.py)

def _git(*a):
    r = subprocess.run(['git'] + list(a), cwd=ROOT, capture_output=True, text=True)
    return r.returncode, r.stdout.strip()


def _frozen_at(rev):
    rc, out = _git('show', f'{rev}:eval/E0b-frozen.json')
    return json.loads(out) if rc == 0 else {}


def registration_commit():
    """The first commit on HEAD's first-parent history whose E0b-frozen.json has registered: true, else None."""
    rc, out = _git('rev-list', '--first-parent', '--reverse', 'HEAD', '--', 'eval/E0b-frozen.json')
    if rc != 0:
        return None
    for c in out.split():
        f = _frozen_at(c)
        if f.get('registered') is True:
            return c
    return None


def registration_ok(sha):
    """(ok, why). `sha` must be the first registered commit, its parent the reviewed candidate R with registered: false,
    the parsed diff exactly {registered: false→true, reviewed: absent→R}, no other path changed, HEAD == sha, and a
    clean tree (`output/` excepted)."""
    first = registration_commit()
    if first is None:
        return False, 'no registered commit on this history'
    rc, full = _git('rev-parse', sha)
    if rc != 0 or full != first:
        return False, f'{sha} is not the first registered commit ({first})'
    rc, parent = _git('rev-parse', f'{full}^')
    a, b = _frozen_at(parent), _frozen_at(full)
    if not a or a.get('registered') is not False:
        return False, 'the parent is not a candidate with registered: false'
    if not b or b.get('reviewed') != parent or 'reviewed' in a:
        return False, 'reviewed must name the parent'
    rest_a = {k: v for k, v in a.items() if k not in ('registered', 'reviewed')}
    rest_b = {k: v for k, v in b.items() if k not in ('registered', 'reviewed')}
    if rest_a != rest_b:
        return False, 'the registration commit changes more than registered and reviewed'
    rc, names = _git('diff', '--name-only', parent, full)
    if names.split() != ['eval/E0b-frozen.json']:
        return False, f'the registration commit touches other paths: {names.split()}'
    rc, head = _git('rev-parse', 'HEAD')
    if head != full:
        return False, f'HEAD {head} is not the registration commit {full}'
    rc, st = _git('status', '--porcelain', '--untracked-files=all', '--', '.', ':!output')
    if st:
        return False, 'the tree is not clean'
    return True, 'ok'


# ---------------------------------------------------------------- the draws

def draw_epoch(r):
    """One epoch's draws, all unconditional, in the form's numbered order."""
    d = {}
    d['npf'] = r.random() < 0.4                                                       # (1)
    d['tail'], d['pk'] = r.randint(20, 40), r.randint(40, 70)                         # (2)
    d['rep'] = r.random() < 0.6                                                        # (3)
    d['O2'], d['repblk'] = r.choice(OFFSETS), r.randint(50, 90)
    d['stale'] = r.random() < 0.5                                                      # (4)
    d['sstart'], d['slen'] = r.randint(10, 60), r.randint(45, 80)
    d['cross'] = r.random() < 0.4                                                      # (5)
    d['cblk'], d['edge'], d['depth'], d['hold'] = r.randint(30, 100), r.randint(0, 1), r.randint(150, 400), r.randint(5, 10)
    d['reb'] = r.random() < 0.2                                                        # (6)
    d['rblk'], d['rsize'], d['rdir'] = r.randint(30, 100), r.randint(50, 100), r.randint(0, 1)
    d['unobs'] = r.random() < 0.2                                                      # (7)
    d['ublk'] = r.randint(30, 100)
    d['flow'] = r.random() < 0.3                                                       # (8)
    d['fblk'], d['fkind'], d['fdep'], d['fwd'] = r.randint(30, 100), r.randint(0, 1), r.randint(100, 2000), r.randint(10, 100)
    d['claim'] = r.random() < 0.2                                                      # (9)
    d['clblk'] = r.randint(30, 100)
    d['ints'] = [r.choice((0, 1, 3)) for _ in range(EPOCH // SUB)]                    # (10)
    return d


# ---------------------------------------------------------------- the clamps (after all draws)

def plan_epoch(d, log, k):
    """Timed ops of one epoch as (start, end, code, args), with the anchors fixed and the optional ops shifted around
    them in the draw order (vault calls also COOLDOWN apart), dropped unless they end by END."""
    ev = []
    if d['npf']:
        tail_end = 2 + d['tail']
        pk = max(d['pk'], tail_end + 1)
        if pk != d['pk']:
            log.append(f'e{k}: poke moved {d["pk"]}->{pk} (after the tail)')
        ev += [(2, 2, SETTLE, ()), (2, tail_end, 'TAIL', ()), (pk, pk, POKE, ())]
    else:
        pk = 22
        ev += [(22, 22, POKE, ())]

    def fits(s, e, code):
        for (a, b, c, _) in ev:
            gap = COOLDOWN if (code in VAULT_OPS and c in VAULT_OPS) else 1
            if not (e + gap <= a or b + gap <= s):
                return False, b + gap
        return True, None

    def place(name, s, length, code, args, min_start=0):
        s = max(s, min_start)
        while True:
            e = s + length
            if e > END:
                log.append(f'e{k}: {name} dropped (does not end by +{END})')
                return
            ok, nxt = fits(s, e, code)
            if ok:
                ev.append((s, e, code, args))
                return
            log.append(f'e{k}: {name} shifted {s}->{nxt}')
            s = nxt

    if d['rep']:
        place('re-placement', d['repblk'], 0, PLACE, (d['O2'],), min_start=pk + COOLDOWN)
    if d['stale']:
        place('stale window', d['sstart'], d['slen'], 'STALE', ())
    if d['cross']:
        place('crossing', d['cblk'], d['hold'], CROSS, (d['edge'], d['depth'], d['hold']))
    if d['reb']:
        place('rebalance', d['rblk'], 0, REBAL, (d['rsize'],), min_start=pk + COOLDOWN)
    if d['unobs']:
        place('unobserved span', d['ublk'], 60, UNOBS, (60,))
    if d['flow']:
        place('flow', d['fblk'], 0, FLOW, (d['fkind'], d['fdep'], d['fwd']))
    if d['claim']:
        place('claim', d['clblk'], 0, CLAIM, ())
    return sorted(ev, key=lambda x: (x[0], x[1]))


def intensity(d, ev, b):
    """The walk intensity at epoch block b: the drawn sub-window value, with the overrides (the tail 3, a stale window
    at least 1; a crossing hold and an unobserved span are not walked)."""
    i = d['ints'][min((b - 1) // SUB if b > 0 else 0, len(d['ints']) - 1)]
    for (s, e, c, _) in ev:                       # a step landing at b covers (b - 2, b]: overrides on (s, e]
        if c == 'TAIL' and s < b <= e:
            i = 3
    for (s, e, c, _) in ev:
        if c == 'STALE' and s < b <= e:
            i = max(i, 1)
    return i


def emit_walk(ops, k, frm, to, d, ev):
    """WALK ops from epoch block frm to to; a step landing at block x takes the intensity of block x (its sub-window and
    the overrides), so runs split where the landing intensity changes (S25C-02)."""
    x = frm + 1
    while x <= to:
        i = intensity(d, ev, x)
        y = x
        while y + 1 <= to and intensity(d, ev, y + 1) == i:
            y += 1
        ops.append([W, k, y, i, 0, 0])
        x = y + 1


def generate(seed, draws=None):
    r = random.Random(seed)
    O = r.choice(OFFSETS)                                                             # epoch 0: O first
    drawn = [draw_epoch(r) for _ in range(EPOCHS)]
    draws = drawn if draws is None else [dict(d) for d in draws]   # `draws` given only by the selftest
    log = []
    # presence by construction (on survival after the clamps): a non-poke-first epoch, a re-placement, a stale window
    # and a crossing; forced into the first epoch from epoch 2 where it survives, else with that epoch's other optional
    # ops dropped
    kinds = (('npf', 'non-poke-first epoch', POKE), ('rep', 're-placement', PLACE), ('stale', 'stale window', 'STALE'),
             ('cross', 'crossing', CROSS))
    OPTIONAL = ('rep', 'stale', 'cross', 'reb', 'unobs', 'flow', 'claim')

    def survives(d, k, key, kind):
        ev = plan_epoch(d, [], k)
        return d['npf'] if key == 'npf' else any(c == kind for (_, _, c, _) in ev)

    REQUIRED = {key: kind for key, _, kind in kinds}

    def present(key):
        return [i for i, d in enumerate(draws) if survives(d, i + 1, key, REQUIRED[key])]

    # S25C3-01: to a fixed point; the strip pass first drops only the non-required optional ops, then required kinds
    # that survive in another epoch; never a required kind whose only survivor is this epoch
    for _round in range(4):
        missing = [(key, name, kind) for key, name, kind in kinds if not present(key)]
        if not missing:
            break
        for key, name, kind in missing:
            done = False
            for strip in (0, 1, 2):
                for i in range(1, EPOCHS):
                    d = dict(draws[i], **{key: True})
                    if strip >= 1:
                        d.update({o: False for o in ('reb', 'unobs', 'flow', 'claim')})
                    if strip == 2:
                        d.update({o: False for o in ('rep', 'stale', 'cross')
                                  if o != key and [j for j in present(o) if j != i]})
                    if survives(d, i + 1, key, kind):
                        draws[i] = d
                        log.append(f'forced {name} into epoch {i + 1}' + (' (optional ops dropped)' if strip else ''))
                        done = True
                        break
                if done:
                    break
            if done:
                continue
            # relocation: a required kind whose only survivor blocks epoch i is first forced into another epoch j
            # (dropping only non-required ops there), then dropped from i
            for i in range(1, EPOCHS):
                d = dict(draws[i], **{key: True}, reb=False, unobs=False, flow=False, claim=False)
                blockers = [o for o in ('rep', 'stale', 'cross') if o != key and d[o]]
                trial = dict(d, **{o: False for o in blockers})
                if not survives(trial, i + 1, key, kind):
                    continue
                moved = {}
                for o in blockers:
                    if [j for j in present(o) if j != i]:
                        continue
                    for j in range(1, EPOCHS):
                        if j == i:
                            continue
                        dj = dict(moved.get(j, draws[j]), **{o: True}, reb=False, unobs=False, flow=False, claim=False)
                        if survives(dj, j + 1, o, REQUIRED[o]) and all(
                                survives(dj, j + 1, q, REQUIRED[q]) for q in ('rep', 'stale', 'cross') if dj[q] and q != o):
                            moved[j] = dj
                            break
                    else:
                        break
                else:
                    for j, dj in moved.items():
                        draws[j] = dj
                        log.append(f'relocated a required op into epoch {j + 1}')
                    draws[i] = trial
                    log.append(f'forced {name} into epoch {i + 1} (blocking ops relocated or dropped)')
                    break
    # surviving optional ops: re-check after forcing (a forced op may still be dropped by its epoch's clamps)
    # epoch 0: poke, placement, then the walk to the epoch's end at intensity 1 around its own target (S25C-02)
    ops = [[W, 0, 22, 1, 0, 0], [POKE, 0, 22, 0, 0, 0], [W, 0, 25, 1, 0, 0], [PLACE, 0, 25, O, 0, 0],
           [W, 0, EPOCH, 1, 0, 0]]
    budget_log = []
    for idx, d in enumerate(draws):
        k = idx + 1
        ev = plan_epoch(d, log, k)
        # swap budget: placements' pre-swaps (1.5% each, worst case) and the rebalance under 4% of NAV
        spent = 0.0
        kept = []
        for x in ev:
            cost = 1.5 if x[2] == PLACE else (x[3][0] / 100.0 if x[2] == REBAL else 0.0)
            if spent + cost > 4.0:
                budget_log.append(f'e{k}: op {x[2]} dropped by the swap budget')
                continue
            spent += cost
            kept.append(x)
        ev = kept
        pos = 0
        for (s, e, c, args) in ev:
            if c == 'TAIL':
                continue
            if c == 'STALE':
                emit_walk(ops, k, pos, s, d, ev)
                ops.append([STALE_ON, k, s, 0, 0, 0])
                emit_walk(ops, k, s, e, d, ev)
                ops.append([STALE_OFF, k, e, 0, 0, 0])
                pos = e
                continue
            emit_walk(ops, k, pos, s, d, ev)
            a = list(args) + [0, 0, 0]
            ops.append([c, k, s, a[0], a[1], a[2]])
            pos = e if c in (CROSS, UNOBS) else s
        emit_walk(ops, k, pos, EPOCH, d, ev)
    for k in (EPOCHS + 1, EPOCHS + 2):                                                  # the closing pokes
        ops += [[W, k, 22, 1, 0, 0], [POKE, k, 22, 0, 0, 0]]
        if k == EPOCHS + 1:
            ops.append([W, k, EPOCH, 1, 0, 0])                                         # epoch 13 walks to its end
    codes = [o[0] for o in ops]
    # S25C-07: the four required kinds survive the clamps and the forcing
    if not (SETTLE in codes and codes.count(PLACE) >= 2 and STALE_ON in codes and CROSS in codes):
        raise SystemExit(f'e0b_gen: seed {seed}: a required op kind did not survive the clamps')
    return dict(seed=seed, ops=[v for op in ops for v in op], n_ops=len(ops), clamps=log + budget_log,
                draws=[{k: v for k, v in d.items()} for d in draws], O=O)


def main(argv):
    if len(argv) < 2:
        print(__doc__)
        return 2
    seed = int(argv[1])
    if seed >= SPENT_BELOW:
        sha = argv[argv.index('--registered') + 1] if '--registered' in argv else None
        ok, why = registration_ok(sha) if sha else (False, 'no --registered <sha>')
        if not ok:
            print(f'e0b_gen: seed {seed} is held out; refused ({why})', file=sys.stderr)
            return 1
    print(json.dumps(generate(seed)))
    return 0


if __name__ == '__main__':
    sys.exit(main(sys.argv))
