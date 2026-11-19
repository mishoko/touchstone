#!/usr/bin/env python3
"""E0b (spec §3.8, §10 "E0b frozen form"; the form `eval/E0b-form.md`): the allocation ledger of §3.8 with the vault's
fee leg read from pool state at each vault call (panel D6), against E0a's fixed-depositor identity, <= 0.3 bp of V per
epoch, on 8 pre-registered held-out scripts (seeds 1100–1107) that stress the witness rate f_w.

  python3 eval/E0b.py repro                 # E0a's ledger and controls reproduced on E0a's pinned data (base='E0a')
  python3 eval/E0b.py validity <dir>        # legality, presence, stress (a)–(c), the alloc count; no residual
  python3 eval/E0b.py fit <dir>             # fit residuals and every control's power (only after `prefit2`)
  python3 eval/E0b.py verdict <dir>         # the held-out verdict (only at the registration commit)
  python3 eval/E0b.py selftest

The ledger is E0a's (`eval/E0a.py`, imported unchanged) with one change, applied to its segments after it runs:
    fee_j  = feeV_j − L_b,j·Δτ_b,j·f_w
    feeV_j = floor(L_j·Δg0/2¹²⁸)·P(p_j¹) + floor(L_j·Δg1/2¹²⁸),  Δg = g(lo_j, hi_j) before the closing call − after
             the opening call (mod 2²⁵⁶), from `E0b-<tag>-feereads.jsonl`; (L_j, lo_j, hi_j) from the closing Checkpoint.
The identity, the drip, the share rows and `power()` are E0a's. Controls are `ctl` branches (as in E0a): E0a's, applied
through E0a's ledger, plus `vault_fee_by_tau` (E0a's vault leg), `vault_fee_opening_L`, `no_passive_fee`,
`prev_fw_whole_window`. Every ledger call decodes its own copy of the logs (E0a's ledger writes into them).
"""
import copy, hashlib, json, math, os, subprocess, sys

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, HERE)
import E0a as A  # noqa: E402
import e0b_gen as G  # noqa: E402
import e0b_run as R  # noqa: E402

BOUND_BP = A.BOUND_BP
Q128 = 1 << 128
M256 = 1 << 256
FIT_RUNS = ('same', 'offset', 'exit')
HELD_OUT = tuple(range(1100, 1108))
E0A_CONTROLS = ('no_alloc', 'lvr_price_leg', 'no_share_terms', 'drip_sign_flipped', 'single_forfeit_drip',
                'witness_shift_one_spacing', 'anchor_closing_price', 'fallback_latest_write', 'fee_by_n',
                'epoch_indexed_witness')
E0B_CONTROLS = ('vault_fee_by_tau', 'vault_fee_opening_L', 'no_passive_fee', 'prev_fw_whole_window')
GATED = ('no_alloc', 'lvr_price_leg', 'no_share_terms', 'drip_sign_flipped', 'single_forfeit_drip',
         'witness_shift_one_spacing', 'anchor_closing_price', 'fallback_latest_write', 'vault_fee_by_tau',
         'vault_fee_opening_L', 'fee_by_n', 'no_passive_fee', 'prev_fw_whole_window')
REPORTED = ('epoch_indexed_witness',)
CH2_FALLBACK = 'prev_fw_whole_window'   # the one stated exception (form §5)
FROZEN = os.path.join(HERE, 'E0b-frozen.json')


def load(p):
    return [json.loads(l) for l in open(p) if l.strip()]


# ---------------------------------------------------------------- the ledger

def fee_reads(rows):
    return {r['c']: r for r in rows}


def vault_fee(s, fr, L=None):
    """feeV of segment s (token1 units at p¹) from the reads after its opening call and before its closing one."""
    L = s['L'] if L is None else L
    if L == 0:
        return 0.0
    a, z = fr[s['open']]['post'], fr[s['close']]['pre']
    assert (a['lo'], a['hi']) == (z['lo'], z['hi']) == (s['lo'], s['hi']), (s['open'], s['close'])
    d0 = (int(z['g0']) - int(a['g0'])) % M256
    d1 = (int(z['g1']) - int(a['g1'])) % M256
    return (L * d0 // Q128) * A.price(s['p1']) + (L * d1 // Q128)


def ledger(raw_logs, tier, feereads, ctl=None, base='E0b'):
    """E0a's ledger on a fresh decode of the logs, then the E0b vault fee leg (unless base='E0a')."""
    logs = [A.decode(copy.deepcopy(x)) for x in raw_logs]
    per_call = {}
    for x in logs:
        if x['ev'] == 'Checkpoint':
            per_call[x['c']] = per_call.get(x['c'], 0) + 1
    bad = [c for c, n in per_call.items() if n > 1]
    assert not bad, f'more than one Checkpoint in vault call(s) {bad[:5]} (a reverted attempt\'s logs were recorded)'
    out, meta = A.ledger(logs, tier, ctl if ctl in E0A_CONTROLS else None)
    if base == 'E0a' or ctl == 'vault_fee_by_tau':
        return out, meta
    fr = fee_reads(feereads)
    segs = meta['segs']
    rolls = meta['rolls']
    # prev_fw_whole_window: the previous lifetime's f_w on every segment of a non-poke-first window
    fw_override = {}
    if ctl == 'prev_fw_whole_window':
        by_call = meta['by_call']
        for e, w in out.items():
            cin_rolled = any(x['ev'] == 'WitnessRolled' and x['lw'] > 0 for x in by_call.get(w['cin'], []))
            if cin_rolled:
                continue
            prev = [r for r in rolls if r['c'] <= w['cin']]
            if prev and prev[-1].get('f') is not None:
                for s in w['segs']:
                    fw_override[id(s)] = prev[-1]['f']
    prev_L = {}
    cps_L = {}
    for s in segs:
        cps_L.setdefault(s['open'], None)
    for i, s in enumerate(segs):
        prev_L[id(s)] = segs[i - 1]['L'] if i > 0 else 0
    for s in segs:
        if s['L'] == 0 or s.get('unresolved'):
            continue
        r = s['roll']
        f = fw_override.get(id(s), r['f'])
        passive = s['Lb'] * s['dtb'] * f
        if ctl == 'no_passive_fee':
            passive = 0.0
        L = prev_L[id(s)] if ctl == 'vault_fee_opening_L' else None
        fv = vault_fee(s, fr, L)
        s['feeV'] = fv
        s['fee'] = fv - passive
        s['alloc'] = 0.0 if ctl == 'no_alloc' else s['price'] + s['fee']
    for e, w in out.items():
        w['alloc'] = sum(s['alloc'] for s in w['segs'])
    return out, meta


def rows_for(d, tag, tier, ctl=None, base='E0b'):
    raw = load(os.path.join(d, f'E0b-{tag}-logs.jsonl'))
    truth = load(os.path.join(d, f'E0b-{tag}-truth.jsonl'))
    fr = load(os.path.join(d, f'E0b-{tag}-feereads.jsonl')) if base == 'E0b' else []
    led, meta = ledger(raw, tier, fr, ctl, base)
    return A.identity(led, truth, ctl if ctl in ('no_share_terms', 'drip_sign_flipped') else None), led, meta, truth


# ---------------------------------------------------------------- guards

def frozen():
    return json.load(open(FROZEN)) if os.path.exists(FROZEN) else {}


def last_record(fz, names):
    recs = [r for r in fz.get('records', []) if r.get('name', '').split('′')[0] in names]
    return recs[-1] if recs else None


def manifests_ok(d, tags, harness_ref, failed=None):
    """Every tag has a manifest whose H hashes equal `harness_ref`, whose tree stayed clean and whose data files match
    (the tier file included). A `passed: false` manifest (the script did not complete) is listed in `failed` when given
    (a FAILED script, form 4.1; S25C-01), else refused; a missing or mismatched manifest is always refused."""
    for t in tags:
        mp = os.path.join(d, f'manifest-{t}.json')
        if not os.path.exists(mp):
            return False, f'no manifest for {t}'
        m = json.load(open(mp))
        if m.get('harness') != harness_ref:
            return False, f'{t}: the harness closure differs from the freeze'
        if m.get('status_after'):
            return False, f'{t}: the tree changed during the run'
        if 'E0b-tier.json' not in m.get('data', {}):
            return False, f'{t}: the tier file is not hashed'
        for f, h in m.get('data', {}).items():
            if R.sha(os.path.join(d, f)) != h:
                return False, f'{t}: {f} changed after the run'
        if not m.get('passed'):
            if failed is None:
                return False, f'{t}: the run did not pass'
            failed.append(t)
    return True, 'ok'


def frozen_at_head():
    r = subprocess.run(['git', 'show', 'HEAD:eval/E0b-frozen.json'], cwd=ROOT, capture_output=True, text=True)
    return json.loads(r.stdout) if r.returncode == 0 else {}


def guard_fit():
    if R.status():
        return False, 'the tree is not clean', None
    fz = frozen_at_head()
    rec = last_record(fz, ('prefit2',))
    if rec is None:
        return False, 'no prefit2 record', None
    here = R.harness_hashes()
    if here != rec['harness']:
        return False, 'the harness closure differs from prefit2', None
    ev = R.eval_hashes()
    if ev['eval/E0a.py'] != rec['eval']['eval/E0a.py']:
        return False, 'eval/E0a.py differs from prefit2', None
    allowed = [rec['eval']['eval/E0b.py']] + [c['eval_after'] for c in fz.get('changes_after_prefit2', [])]
    if ev['eval/E0b.py'] != allowed[-1]:
        return False, 'eval/E0b.py differs from prefit2 and from the last listed change', None
    return True, 'ok', rec


def is_fit_data(d):
    """Content check: every fit run reproduces E0a's pinned data (truth identical, logs up to the vault address)."""
    return all(R.fit_matches_e0a(d, r)[0] for r in FIT_RUNS)


# ---------------------------------------------------------------- the witness series: block spans (form §3.2, §4.2)

def first_touch_rows(wit):
    """Per touched block, the boundary (g0, g1, tauB, wlo, whi): a `step` row, or for a vault call that is the block's
    first touch, g from its `pre` row and tauB from its `post` row."""
    out = {}
    by_b = {}
    for r in wit:
        by_b.setdefault(r['b'], []).append(r)
    for b, rs in sorted(by_b.items()):
        r0 = rs[0]
        if r0['kind'] == 'step':
            out[b] = dict(g0=int(r0['g0']), g1=int(r0['g1']), tauB=r0['tauB'], wlo=r0['wlo'], whi=r0['whi'],
                          tick=r0['tick'])
        elif r0['kind'] == 'pre':
            post = next(x for x in rs if x['kind'] == 'post' and x['c'] == r0['c'])
            out[b] = dict(g0=int(r0['g0']), g1=int(r0['g1']), tauB=post['tauB'], wlo=r0['wlo'], whi=r0['whi'],
                          tick=r0['tick'], post_range=(post['wlo'], post['whi']))
    return out


def spans(wit):
    """Block spans [b, next) between consecutive first-touch rows on an unchanged witness range: (b, nb, fees per unit
    L at the span's start tick, dtauB). A span whose range changes is excluded."""
    ft = first_touch_rows(wit)
    bs = sorted(ft)
    out = []
    for b, nb in zip(bs, bs[1:]):
        x, y = ft[b], ft[nb]
        # g from the boundary row (a vault call's `pre` row: its pre-swap fees fall in the span it opens); a span
        # whose witness range changes (a roll in the opening call, or before the closing row) is excluded
        rng = (x['wlo'], x['whi'])
        if rng != (y['wlo'], y['whi']) or x.get('post_range', rng) != rng:
            continue
        d0, d1 = (y['g0'] - x['g0']) % M256, (y['g1'] - x['g1']) % M256
        fee = (d0 * A.price(x['tick']) + d1) / Q128
        out.append(dict(b=b, nb=nb, fee=fee, dtau=y['tauB'] - x['tauB'], rng=rng))
    return out


def op_counts(ops, wit):
    """Stale windows and crossings that COUNT: ΔtauB < Δb − 4 between the first-touch rows of their opening and closing
    touched blocks."""
    ft = first_touch_rows(wit)
    bs = sorted(ft)

    def touch_at_or_after(b):
        return next((x for x in bs if x >= b), None)
    res = dict(stale=0, cross=0)
    on = None
    for o in ops:
        if o.get('op') == 4:
            on = o['b0']
        elif o.get('op') == 5 and on is not None:
            a, z = touch_at_or_after(on), touch_at_or_after(o['b0'] + 1)
            if a is not None and z is not None and ft[z]['tauB'] - ft[a]['tauB'] < (z - a) - 4:
                res['stale'] += 1
            on = None
        elif o.get('op') == 6:
            a, z = o['b0'], o['b1']
            if a in ft and z in ft and ft[z]['tauB'] - ft[a]['tauB'] < (z - a) - 4:
                res['cross'] += 1
    return res


# ---------------------------------------------------------------- validity (no identity, no residual)

def validity_one(d, tag, tier):
    ops = load(os.path.join(d, f'E0b-{tag}-ops.jsonl'))
    wit = load(os.path.join(d, f'E0b-{tag}-witness.jsonl'))
    truth = load(os.path.join(d, f'E0b-{tag}-truth.jsonl'))
    raw = load(os.path.join(d, f'E0b-{tag}-logs.jsonl'))
    fr = load(os.path.join(d, f'E0b-{tag}-feereads.jsonl'))
    completed = bool(ops) and ops[-1].get('op') == 99
    reverts = sum(1 for o in ops if o.get('op') == 'revert')
    calls = len(truth)
    T = {t['c']: t for t in truth}
    led, meta = ledger(raw, tier, fr)
    # presence
    npf = sum(1 for e, w in led.items() if w['cin'] in T and T[w['cin']]['k'] != A.K_POKE)
    regs = [x for x in meta['by_call'].values() for x in x if x['ev'] == 'RangeRegistered']
    rolls = meta['rolls']
    replace_in_life = 0
    for x in regs:
        prior = [r for r in rolls if r['c'] < x['c']]
        seg_before = [s for s in meta['segs'] if s['close'] == x['c']]
        if prior and seg_before and seg_before[0]['L'] > 0:
            replace_in_life += 1
    oc = op_counts(ops, wit)
    sp = spans(wit)
    # (a) true passive fees per epoch, bp of V (truth L_b; φ from the segment's own witness reads)
    witc = {}
    for r in wit:
        if r['kind'] in ('pre', 'post'):
            witc[(r['c'], r['kind'])] = r
    a_max = 0.0
    alloc_hits = 0
    for e, w in led.items():
        if w['cin'] not in T or w['cout'] not in T:
            continue
        Dd = int(T[w['cin']]['D'])
        V = int(T[w['cin']]['nav']) * Dd / int(T[w['cin']]['S'])
        tot = 0.0
        al = 0.0
        for s in w['segs']:
            seg = T[s['close']]['seg']
            if not seg:
                continue
            a, z = witc.get((s['open'], 'post')), witc.get((s['close'], 'pre'))
            if a and z and (a['wlo'], a['whi']) == (z['wlo'], z['whi']) == (seg['wlo'], seg['whi']):
                d0 = (int(z['g0']) - int(a['g0'])) % M256
                d1 = (int(z['g1']) - int(a['g1'])) % M256
                tot += Dd / int(seg['S']) * int(seg['Lb']) * (d0 * A.price(seg['p1']) + d1) / Q128
            al += Dd / int(seg['S']) * s['alloc']
        a_max = max(a_max, tot / V * 1e4)
        if abs(al) / V * 1e4 >= 10 * BOUND_BP:
            alloc_hits += 1
    # (b), (c): per lifetime (between successful rolls), spans assigned to segments by their start block
    b_ok = c_ok = False
    rb = [r['b'] for r in rolls]
    for lo_b, hi_b in zip(rb, rb[1:]):
        life = [x for x in sp if lo_b <= x['b'] < hi_b]
        tot = sum(x['fee'] for x in life)
        unc = sum(x['fee'] for x in life if x['dtau'] == 0)
        if tot > 0 and unc / tot >= 0.10:
            c_ok = True
        segs = [s for s in meta['segs'] if lo_b <= s['b0'] < hi_b]
        rates = []
        for s in segs:
            xs = [x for x in life if s['b0'] <= x['b'] < s['b1']]
            cred, fee = sum(x['dtau'] for x in xs), sum(x['fee'] for x in xs if x['dtau'] > 0)
            if cred >= 10 and fee > 0:
                rates.append(fee / cred)
        if len(rates) >= 2 and max(rates) >= 2 * min(rates):
            b_ok = True
    return dict(completed=completed, reverts=reverts, vault_calls=calls,
                reverted_share=reverts / max(reverts + calls, 1), non_poke_first=npf, replacements_in_lifetime=replace_in_life,
                stale_counting=oc['stale'], cross_counting=oc['cross'], a_max_bp=a_max, a=a_max >= 10 * BOUND_BP,
                b=b_ok, c=c_ok, alloc_epochs=alloc_hits, alloc=alloc_hits >= 1,
                presence=bool(npf and replace_in_life and oc['stale'] and oc['cross']))


def validity(d, harness_ref=None, failed=None):
    tier = {k: int(v) for k, v in json.load(open(os.path.join(d, 'E0b-tier.json'))).items()}
    # every script whose data are present needs its manifest (derived from the data files, never from the manifests)
    tags = sorted({f[len('E0b-'):f.index('-', len('E0b-e0b-'))] for f in os.listdir(d)
                   if f.startswith('E0b-e0b-') and f.endswith('-logs.jsonl')} |
                  {f[len('manifest-'):-len('.json')] for f in os.listdir(d) if f.startswith('manifest-e0b-')})
    if not tags:
        raise SystemExit('E0b validity: refused (no script data)')
    # the data must come from the harness of the last record (prefit, or the last logged change that touched H)
    fz = frozen()
    ref = harness_ref or ([c['harness'] for c in fz.get('changes', []) if c.get('harness')] or
                          [r['harness'] for r in fz.get('records', []) if r['name'] == 'prefit'] or [None])[-1]
    failed = [] if failed is None else failed
    ok, why = manifests_ok(d, tags, ref, failed)
    if not ok:
        raise SystemExit(f'E0b validity: refused ({why})')
    res = {}
    for t in tags:
        try:
            res[t] = validity_one(d, t, tier)
        except Exception as ex:                      # an incomplete script's files: a FAILED script, never a refusal
            res[t] = dict(completed=False, reverted_share=1.0, presence=False, a=False, b=False, c=False, alloc=False,
                          error=str(ex)[:200])
        if t in failed:
            res[t]['completed'] = False
    n = len(res)
    rate = lambda k: sum(1 for v in res.values() if v[k]) / n if n else 0.0

    def p_at_least(m, N, p):
        return sum(math.comb(N, i) * p ** i * (1 - p) ** (N - i) for i in range(m, N + 1))
    summary = dict(seeds=n, all_complete=all(v['completed'] and v['reverted_share'] <= 0.25 for v in res.values()),
                   all_presence=all(v['presence'] for v in res.values()),
                   rates={k: rate(k) for k in ('a', 'b', 'c', 'alloc')},
                   p_heldout={k: p_at_least(6 if k != 'alloc' else 4, 8, rate(k)) for k in ('a', 'b', 'c', 'alloc')})
    summary['accept'] = (summary['all_complete'] and summary['all_presence'] and
                         all(summary['rates'][k] * n >= 19 for k in ('a', 'b', 'c')) and summary['rates']['alloc'] * n >= 16)
    return res, summary


# ---------------------------------------------------------------- fit and verdict

def evaluate_runs(d, tags, tier, controls=True):
    res = {}
    for t in tags:
        rows, led, meta, truth = rows_for(d, t, tier)
        r = dict(rows=rows, worst_bp=max((abs(x['residual_bp']) for x in rows), default=0.0),
                 unresolved=[x['e'] for x in rows if x['unresolved']])
        if controls:
            r['controls'] = {}
            for c in GATED + REPORTED:
                cr = rows_for(d, t, tier, ctl=c)[0]
                r['controls'][c] = dict(worst_bp=max((abs(x['residual_bp']) for x in cr), default=0.0),
                                        power=A.power(rows, cr))
        res[t] = r
    return res


def fit(d):
    ok, why, rec = guard_fit()
    if not ok:
        raise SystemExit(f'E0b fit: refused ({why})')
    if not is_fit_data(d):
        raise SystemExit('E0b fit: the data are not the fit runs (content check)')
    ok, why = manifests_ok(d, [f'fit-{r}' for r in FIT_RUNS], rec['harness'])
    if not ok:
        raise SystemExit(f'E0b fit: refused ({why})')
    tier = {k: int(v) for k, v in json.load(open(os.path.join(d, 'E0b-tier.json'))).items()}
    res = evaluate_runs(d, [f'fit-{r}' for r in FIT_RUNS], tier)
    powered = {c: any(res[t]['controls'][c]['power']['exceeds_on_closing_rows'] for t in res) for c in GATED + REPORTED}
    out = dict(eval='E0b-fit', bound_bp=BOUND_BP, record=rec['name'], commit=rec.get('commit'),
               runs={t: dict(worst_bp=v['worst_bp'], rows=v['rows'], unresolved=v['unresolved']) for t, v in res.items()},
               controls={c: {t: res[t]['controls'][c] for t in res} for c in GATED + REPORTED},
               powered=powered, gated=list(GATED), reported=list(REPORTED),
               note='fit data (E0a scripts); the exit e = 5 row closing under E0b is by construction (the read is '
                    "the truth's own quantity) and is not evidence")
    return out


def verdict(d):
    head = R.head()
    ok, why = G.registration_ok(head)
    if not ok:
        raise SystemExit(f'E0b verdict: refused ({why})')
    fz = frozen()
    rec = last_record(fz, ('prefit2',))
    tags = [f'e0b-{n}' for n in HELD_OUT]
    # manifests committed on e0b after registration (post-registration allowance)
    for n in HELD_OUT:
        r = subprocess.run(['git', 'show', f'e0b:eval/results/E0b-manifests/{n}.json'], cwd=ROOT, capture_output=True,
                           text=True)
        if r.returncode != 0:
            raise SystemExit(f'E0b verdict: manifest {n} is not committed on e0b')
        committed = json.loads(r.stdout)
        local = json.load(open(os.path.join(d, f'manifest-e0b-{n}.json')))
        if committed != local:
            raise SystemExit(f'E0b verdict: manifest {n} differs from the committed one')
        if committed.get('commit') != head or committed.get('seed') != n:
            raise SystemExit(f'E0b verdict: manifest {n} is not from the registration commit')
        script = json.dumps(G.generate(n))
        if hashlib.sha256(script.encode()).hexdigest() != committed.get('script_sha256'):
            raise SystemExit(f'E0b verdict: seed {n} script does not regenerate')
    extra = [f for f in os.listdir(d) if f.startswith('manifest-e0b-') and f not in {f'manifest-{t}.json' for t in tags}]
    if extra:
        raise SystemExit(f'E0b verdict: manifests outside 1100–1107: {extra}')
    rc = subprocess.run(['git', 'diff', '--name-only', head, 'e0b'], cwd=ROOT, capture_output=True, text=True).stdout.split()
    bad = [p for p in rc if not (p.startswith('eval/results/E0b-manifests/') or p == 'eval/results/E0b.json')]
    if bad:
        raise SystemExit(f'E0b verdict: commits after registration touch {bad}')
    failed = []
    ok, why = manifests_ok(d, tags, rec['harness'], failed)
    if not ok:
        raise SystemExit(f'E0b verdict: refused ({why})')
    return judge(d, tags, rec, failed, fz, head)


def judge(d, tags, rec, failed, fz, head):
    """The section 4 rule on manifest-checked data; a FAILED script makes the verdict FAIL (S25C-01)."""
    tier = {k: int(v) for k, v in json.load(open(os.path.join(d, 'E0b-tier.json'))).items()}
    vres, vsum = validity(d, rec['harness'], list(failed))
    failed_scripts = sorted(set(failed) | {t for t, v in vres.items() if not v['completed'] or v['reverted_share'] > 0.25})
    ok_tags = [t for t in tags if t not in failed_scripts]
    res = evaluate_runs(d, ok_tags, tier, controls=False)
    # rows that 3.8 leaves unresolved are excluded from closure and counted by condition 3 (S25C-06 c)
    closes = all(abs(x['residual_bp']) <= BOUND_BP for v in res.values() for x in v['rows'] if not x['unresolved'])
    share_ok = True  # the share rows (E0a's check) per script
    for t in ok_tags:
        rows, led, meta, truth = rows_for(d, t, tier)
        T = {x['c']: x for x in truth}
        for e, w in led.items():
            if w['cin'] not in T:
                continue
            tm = sum(int(T[c]['mints']) for c in w['calls'])
            tb = sum(int(T[c]['burns']) for c in w['calls'])
            if w['minted'] != tm or w['charges'] + sum(w['drip'].values()) != tb:
                share_ok = False
    unresolved_ok = all(len(v['unresolved']) <= 2 for v in res.values())
    nv = dict(presence=all(v['presence'] for v in vres.values()),
              a=sum(v['a'] for v in vres.values()) >= 6, b=sum(v['b'] for v in vres.values()) >= 6,
              c=sum(v['c'] for v in vres.values()) >= 6, alloc=sum(v['alloc'] for v in vres.values()) >= 4)
    powered = fz.get('control_powers', {})
    ctl_ok = all(powered.get(c) for c in GATED if c != CH2_FALLBACK) and (
        powered.get(CH2_FALLBACK) or fz.get('ch2_unpowered_recorded'))
    v = (not failed_scripts) and closes and share_ok and unresolved_ok and all(nv.values()) and ctl_ok
    return dict(eval='E0b', bound_bp=BOUND_BP, verdict='PASS' if v else 'FAIL', registration=head,
                failed_scripts=failed_scripts, closes=closes, share_rows_close=share_ok, unresolved_ok=unresolved_ok,
                nonvacuity=nv, controls_powered_at_registration=ctl_ok, validity=vres,
                runs={t: dict(worst_bp=r['worst_bp'], rows=r['rows'], unresolved=r['unresolved']) for t, r in res.items()},
                wording_pass=('the allocation ledger, with the vault\'s fee leg read from pool state at each vault call, '
                              'closes within 0.3 bp of a fixed depositor\'s value per epoch on 8 pre-registered scripts '
                              'that stress the witness rate (TEST tier)'),
                wording_fail='H, the ledger\'s closure claim is withdrawn and it stays disclosure with no guarantee; '
                             'no third attempt')


# ---------------------------------------------------------------- repro (E0a's own data, base='E0a')

def repro():
    d = os.path.join(ROOT, 'output', 'e0a')
    pins = dict(l.split()[::-1] for l in open(R.E0A_PIN_FILE) if l.strip())
    for f, h in pins.items():
        assert R.sha(os.path.join(d, f)) == h, f'{f} does not match its pin'
    tier = {k: int(v) for k, v in json.load(open(os.path.join(d, 'E0a-tier.json'))).items()}
    n = 0
    for run in FIT_RUNS:
        raw = load(os.path.join(d, f'E0a-{run}-logs.jsonl'))
        truth = load(os.path.join(d, f'E0a-{run}-truth.jsonl'))
        for c in (None,) + E0A_CONTROLS:
            ref_led, _ = A.ledger([A.decode(copy.deepcopy(x)) for x in raw], tier, c)
            ref = A.identity(ref_led, truth, c if c in ('no_share_terms', 'drip_sign_flipped') else None)
            mine_led, _ = ledger(raw, tier, [], c, base='E0a')
            mine = A.identity(mine_led, truth, c if c in ('no_share_terms', 'drip_sign_flipped') else None)
            assert [x['residual_bp'] for x in ref] == [x['residual_bp'] for x in mine], (run, c)
            n += 1
    return n


# ---------------------------------------------------------------- selftest

def selftest():
    # isolation: the base rows are bit-identical before and after every control runs (fit data, dev directory)
    d = os.path.join(ROOT, 'output', 'e0b', 'dev')
    if os.path.exists(os.path.join(d, 'E0b-fit-exit-logs.jsonl')):
        tier = {k: int(v) for k, v in json.load(open(os.path.join(d, 'E0b-tier.json'))).items()}
        raw = load(os.path.join(d, 'E0b-fit-exit-logs.jsonl'))
        fr = load(os.path.join(d, 'E0b-fit-exit-feereads.jsonl'))
        a, _ = ledger(raw, tier, fr)
        before = [w['alloc'] for _, w in sorted(a.items())]
        for c in GATED + REPORTED:
            ledger(raw, tier, fr, c)
        b, _ = ledger(raw, tier, fr)
        assert before == [w['alloc'] for _, w in sorted(b.items())], 'ledger isolation'
    # the block-span rule (synthetic witness rows)
    def row(b, kind, g, tau, c=0, rng=(-1000, 1000), tick=0):
        return dict(b=b, kind=kind, c=c, wlo=rng[0], whi=rng[1], g0=str(0), g1=str(g), tauB=tau, tick=tick)
    # an in-range walk with a vault call (pre-swap fees between pre and post) in a walked block: (c) = 0
    w = [row(10, 'step', 0, 0), row(10, 'pre', 5, 0, 1), row(10, 'post', 9, 0, 1), row(12, 'step', 12, 2),
         row(14, 'step', 20, 4)]
    assert all(x['dtau'] > 0 for x in spans(w)), spans(w)
    # a vault call as its block's first touch after a hold: its pre-swap fees fall in the span it opens (credited)
    w = [row(10, 'step', 0, 0), row(16, 'pre', 0, 0, 1), row(16, 'post', 7, 6, 1), row(18, 'step', 9, 8)]
    sp = spans(w)
    assert sp[0]['dtau'] == 6 and sp[0]['fee'] == 0 and sp[1]['fee'] * Q128 == 9 and sp[1]['dtau'] == 2, sp
    # an out-of-range hold: (c) > 0; the crossing with Δb = 5 counts
    w = [row(10, 'step', 0, 0), row(15, 'step', 50, 0), row(17, 'step', 52, 2)]
    sp = spans(w)
    assert sp[0]['dtau'] == 0 and sp[0]['fee'] > 0
    assert op_counts([dict(op=6, b0=10, b1=15)], w)['cross'] == 1
    assert op_counts([dict(op=6, b0=15, b1=17)], w)['cross'] == 0
    # a range change inside a span is excluded
    w = [row(10, 'step', 0, 0), row(12, 'step', 3, 2, rng=(-990, 1010))]
    assert spans(w) == []
    # the guards: fit refuses without a prefit2 record; non-fit data are refused by content
    if not frozen().get('records'):
        assert guard_fit()[0] is False
    assert G.SPENT_BELOW == 1100 and G.main(['x', '1100']) == 1
    # the registration guard (S25E3-34) in a scratch repository: the first flip after a reviewed candidate is accepted;
    # a second flip, a flip carrying another change, and a flip whose parent is not the candidate are refused
    import tempfile
    root0 = G.ROOT
    with tempfile.TemporaryDirectory() as tmp:
        def git(*a):
            subprocess.run(['git', '-c', 'user.name=t', '-c', 'user.email=t@t', '-c', 'commit.gpgsign=false'] + list(a),
                           cwd=tmp, check=True, capture_output=True)

        def put(obj, msg):
            os.makedirs(os.path.join(tmp, 'eval'), exist_ok=True)
            json.dump(obj, open(os.path.join(tmp, 'eval', 'E0b-frozen.json'), 'w'), indent=1, sort_keys=True)
            git('add', '-A')
            git('commit', '-q', '-m', msg)
            return subprocess.run(['git', 'rev-parse', 'HEAD'], cwd=tmp, capture_output=True, text=True).stdout.strip()
        G.ROOT = tmp
        try:
            git('init', '-q')
            cand = put({'registered': False, 'x': 1}, 'R')
            reg = put({'registered': True, 'reviewed': cand, 'x': 1}, 'flip')
            assert G.registration_ok(reg)[0], G.registration_ok(reg)
            later = put({'registered': True, 'reviewed': reg, 'x': 1}, 'second flip')
            assert not G.registration_ok(later)[0]                       # HEAD moved; and not the first flip
            git('reset', '-q', '--hard', cand)
            bad = put({'registered': True, 'reviewed': cand, 'x': 2}, 'flip with another change')
            assert not G.registration_ok(bad)[0]
            git('reset', '-q', '--hard', cand)
            mid = put({'registered': False, 'x': 1, 'y': 0}, 'another candidate')
            wrong = put({'registered': True, 'reviewed': cand, 'x': 1, 'y': 0}, 'flip naming the wrong parent')
            assert not G.registration_ok(wrong)[0]
        finally:
            G.ROOT = root0
    # S25C-02: every walk op starts inside its own epoch (so the target has that epoch's parity), on seeds 0–19
    for seed in range(20):
        ops = G.generate(seed)['ops']
        pos = 0
        for i in range(0, len(ops), 6):
            code, k, off = ops[i], ops[i + 1], ops[i + 2]
            blk = k * G.EPOCH + off
            if code == G.W:
                assert pos >= k * G.EPOCH, (seed, i, pos, k)
            if code == G.UNOBS:
                blk += ops[i + 3]
            if code == G.CROSS:
                blk += ops[i + 5]
            pos = max(pos, blk)
    # S25C-04: a call with two Checkpoints (a reverted attempt's logs) makes the ledger raise; the unmodified data pass
    if os.path.exists(os.path.join(d, 'E0b-fit-exit-logs.jsonl')):
        raw = load(os.path.join(d, 'E0b-fit-exit-logs.jsonl'))
        cp = next(x for x in raw if x['ev'] == 'Checkpoint')
        try:
            ledger(raw + [dict(cp, i=cp['i'] + 1000)], tier, fr)
            raise AssertionError('a duplicated Checkpoint was accepted')
        except AssertionError as ex:
            assert 'more than one Checkpoint' in str(ex), ex
    # S25C-01 and M2: an unmanifested directory is refused; a not-passed manifest is a FAILED script and the verdict is
    # FAIL (judge), never a refusal
    import tempfile, shutil
    src = os.path.join(ROOT, 'output', 'e0b', 'smoke')
    if os.path.exists(os.path.join(src, 'manifest-e0b-0.json')):
        with tempfile.TemporaryDirectory() as tmp:
            for f in os.listdir(src):
                if f.startswith('E0b-e0b-0-') or f == 'E0b-tier.json':
                    shutil.copy(os.path.join(src, f), tmp)
            try:
                validity(tmp, harness_ref={'x': 'y'})
                raise AssertionError('an unmanifested directory was accepted')
            except SystemExit as ex:
                assert 'no manifest' in str(ex), ex
            m = json.load(open(os.path.join(src, 'manifest-e0b-0.json')))
            ops = os.path.join(tmp, 'E0b-e0b-0-ops.jsonl')
            open(ops, 'w').write(''.join(open(ops).readlines()[:5]))       # an incomplete script: no op 99
            m['passed'] = False
            m['data'] = {f: R.sha(os.path.join(tmp, f)) for f in os.listdir(tmp)}
            json.dump(m, open(os.path.join(tmp, 'manifest-e0b-0.json'), 'w'))
            out = judge(tmp, ['e0b-0'], dict(harness=m['harness']), [], {}, 'x')
            assert out['verdict'] == 'FAIL' and out['failed_scripts'] == ['e0b-0'], out['failed_scripts']
    print('E0b selftest OK')
    return 0


def main(argv):
    if len(argv) < 2:
        print(__doc__)
        return 2
    cmd = argv[1]
    if cmd == 'repro':
        print(f'E0b repro: {repro()} (run, control) pairs reproduce E0a bit for bit')
        return 0
    if cmd == 'selftest':
        return selftest()
    if cmd == 'validity':
        res, summ = validity(argv[2])
        for t, v in sorted(res.items()):
            print(t, {k: (round(x, 3) if isinstance(x, float) else x) for k, x in v.items()})
        print(json.dumps(summ, indent=1))
        return 0 if summ['accept'] else 1
    if cmd == 'fit':
        out = fit(argv[2])
        json.dump(out, open(os.path.join(HERE, 'results', 'E0b-fit.json'), 'w'), indent=1, default=str)
        for t, r in out['runs'].items():
            print(f"{t}: worst |residual| {r['worst_bp']:.4f} bp")
        for c in GATED + REPORTED:
            w = max(v['worst_bp'] for v in out['controls'][c].values())
            print(f"  {c}: worst {w:.4f} bp, {'powered' if out['powered'][c] else 'NOT powered'} on a closing row"
                  f"{' (gated)' if c in GATED else ' (reported)'}")
        return 0
    if cmd == 'verdict':
        out = verdict(argv[2])
        json.dump(out, open(os.path.join(HERE, 'results', 'E0b.json'), 'w'), indent=1, default=str)
        print(f"E0b {out['verdict']}")
        return 0 if out['verdict'] == 'PASS' else 1
    print(__doc__)
    return 2


if __name__ == '__main__':
    sys.exit(main(sys.argv))
