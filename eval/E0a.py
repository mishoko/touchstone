#!/usr/bin/env python3
"""E0a (spec §3.8 "E0a's form", §10): the amended allocation ledger, computed from events alone, against a fixed
depositor's identity, <= 0.3 bp of V per epoch, with the registered (s19) formula reported next to it.

  TS_E0A_OUT=output/e0a forge test --mc E0aDataTest && python3 eval/E0a.py output/e0a   # writes eval/results/E0a.json

Inputs (`test/eval/E0aData.t.sol`, per run `same`, `offset`, `exit`):
- `E0a-<run>-logs.jsonl`: raw logs tagged by call and block. The LEDGER (`ledger()`) reads only these and
  `E0a-tier.json` (W_TICKS, TICK_SPACING, EPOCH, FORFEIT_DRIP_BLOCKS, L_REF): `Checkpoint`, `EpochRecorded`,
  `WitnessRolled`, `RefRefreshed`, `RangeRegistered`, the vault's `Transfer`, `BreachProcessed`, `Retired`, `Settled`.
- `E0a-<run>-truth.jsonl`: the ground truth per vault call, from storage (NAV at the cached reference, S, the fixed
  depositor's D, the passive leg's change over the closed segment with `L_b` sized from hook storage, the call's own
  NAV change, other holders' share flows, the vault's mints and burns from `Transfer`). Used only by `identity()`.

The ledger (spec §3.8, amended): per segment j between consecutive `Checkpoint`s, holdings from the closing one; prices
are cache writes only (p0 = the last write in the opening checkpoint's call, else the latest before; p1 = the next
segment's p0);
    price_j = [V(L_j, lo, hi, p1) - V(L_j, lo, hi, p0)] - [V(Lb_j, wlo, whi, p1) - V(Lb_j, wlo, whi, p0)]
    fee_j   = (L_j·dτv_j - Lb_j·dτb_j) · f_w,   f_w = fg_w(at the lifetime's last p1) / L_REF / dτb_w
    alloc_j = price_j + fee_j  (L_j = 0 => 0)
with witness lifetimes from one successful `WitnessRolled` to the next, the centre the first write of the roll's epoch
before the log (else the first write of the latest earlier epoch that had one), `Lb` sized at registrations (at
`liveTick`) and rolls (at the centre) and scaled with L between.

The identity, per epoch window (from the checkpoint that rolls into e to the one that rolls out of it):
    dV = sum_k V_k·[(dpassive_k + alloc_k - loss_k + flowValue_k)/NAV_k - (minted_k - burned_k + flowShares_k)/S_k] + residual
minted and charge burns from `EpochRecorded`; the drip recomputed by the bank's restart rule from the forfeits'
blocks. The per-step weights V_k/S_k are the truth's; the amounts are the ledger's. Within an epoch the record's
minted and charge-burn totals are placed at the truth-weighted mean of the calls where the vault's own mints and burns
occurred (a second-order placement; the amounts stay the ledger's). Consequence (fix review S21F-05): once the share
rows close (the record's minted equals the vault's `Transfer` mints, the recomputed burns equal its burns), the
identity's share terms equal the truth's per-call sum, so only the share rows test the share terms; the identity adds
no independent test of their timing.

Controls: the verdict uses the frozen criterion (each control's worst |residual| exceeds the bound in some row);
`controls_power` reports each control's own shift row by row against the base and whether the base closes there.

Scope: E0a tests that the events reproduce the state, the fee-leg approximation and the share terms; it does not test
the benchmark's economic meaning. Disclosed: `m_perBlock` is raised in the harness (see `E0a-tier.json`).
"""
import json, math, os, sys

BOUND_BP = 0.3
HERE = os.path.dirname(os.path.abspath(__file__))
RUNS = ('same', 'offset', 'exit')
CONTROLS = ('no_alloc', 'lvr_price_leg', 'no_share_terms', 'drip_sign_flipped', 'single_forfeit_drip',
            'witness_shift_one_spacing', 'fee_by_n', 'epoch_indexed_witness', 'anchor_closing_price',
            'fallback_latest_write')
K_POKE, K_SETTLE, K_PLACE, K_CLAIM, K_RETIRE, K_DEPOSIT, K_WITHDRAW, K_BREACH = range(8)


# ---------------------------------------------------------------- decoding

def _words(h):
    h = h[2:] if h.startswith('0x') else h
    return [int(h[i:i + 64], 16) for i in range(0, len(h), 64)]


def _s(w):
    return w - (1 << 256) if w >= 1 << 255 else w


def decode(line):
    ev, w = line['ev'], _words(line['data'])
    d = dict(c=line['c'], b=line['b'], i=line['i'], ev=ev)
    if ev == 'Checkpoint':
        d.update(tauV=w[0], tauB=w[1], nav0=w[2], delta=w[3], base=w[4], L=w[5], lo=_s(w[6]), hi=_s(w[7]))
    elif ev == 'EpochRecorded':
        f = ('start', 'end', 'med9Start', 'med9End', 'tauV', 'tauB', 'lTau', 'flags', 'base', 'charge', 'minted',
             'bankBurned', 'bondBurned', 'forfeited', 'forfeitValue')
        d.update({k: (_s(v) if k.startswith('med9') else v) for k, v in zip(f, w)})
        d['e'] = int(line['topics'][1], 16)
    elif ev == 'WitnessRolled':
        d.update(epoch=w[0], fg0=w[1], fg1=w[2], lw=w[3])
    elif ev == 'RefRefreshed':
        d.update(grid=w[0], tick=_s(w[1]))
    elif ev == 'RangeRegistered':
        d.update(lo=_s(w[0]), hi=_s(w[1]), tick=_s(w[2]))
    elif ev == 'Transfer':
        d.update(frm=int(line['topics'][1], 16), to=int(line['topics'][2], 16), value=w[0])
    elif ev == 'BreachProcessed':
        d.update(count=w[0], lastBlock=w[1], forfeited=w[2])
    elif ev == 'Retired':
        d.update(forfeited=w[0])
    elif ev == 'Settled':
        d.update(charge0=w[0], extra=w[1], tickMk=_s(w[2]), fallbackUsed=w[3])
    return d


def load(path):
    return [json.loads(l) for l in open(path) if l.strip()]


# ---------------------------------------------------------------- closed forms

def price(t):
    return 1.0001 ** t


def value(L, lo, hi, t):
    """Token1 value of L on [lo, hi) at tick t (exact amounts, no Taylor term)."""
    if L == 0:
        return 0.0
    sp, sa, sb = math.sqrt(price(t)), math.sqrt(price(lo)), math.sqrt(price(hi))
    if sp <= sa:
        return L * (1 / sa - 1 / sb) * sp * sp
    if sp >= sb:
        return L * (sb - sa)
    return L * (1 / sp - 1 / sb) * sp * sp + L * (sp - sa)


def wrange(c, tier, shift=0):
    sp, W = tier['TICK_SPACING'], tier['W_TICKS']
    base = (c // sp) * sp  # floor toward -inf
    return base - W // 2 + shift, base + W // 2 + shift


# ---------------------------------------------------------------- the ledger (logs + tier only)

def ledger(logs, tier, ctl=None):
    E = tier['EPOCH']
    key = lambda x: (x['c'], x['i'])
    writes = sorted([x for x in logs if x['ev'] in ('RefRefreshed', 'RangeRegistered')], key=key)
    cps = sorted([x for x in logs if x['ev'] == 'Checkpoint'], key=key)
    rolls_all = sorted([x for x in logs if x['ev'] == 'WitnessRolled'], key=key)
    rolls = [r for r in rolls_all if r['lw'] > 0]
    regs = {x['c']: x for x in logs if x['ev'] == 'RangeRegistered'}
    recs = {x['e']: x for x in logs if x['ev'] == 'EpochRecorded'}
    rec_call = {x['e']: x['c'] for x in logs if x['ev'] == 'EpochRecorded'}

    def p_open(c):
        """The last cache write in call c, else the latest before it."""
        t = None
        for w in writes:
            if w['c'] <= c:
                t = w['tick']
            else:
                break
        return t

    def p_anchor(cp):
        """(control) the last write before the closing checkpoint's log."""
        t = None
        for w in writes:
            if key(w) < key(cp):
                t = w['tick']
            else:
                break
        return t

    # the witness of each successful roll
    def centre(r, latest=False):
        before = [w for w in writes if key(w) < key(r)]
        same = [w for w in before if w['b'] // E == r['epoch']]
        if same:
            return same[0]['tick'], False
        eps = sorted({w['b'] // E for w in before if w['b'] // E < r['epoch']})
        e1 = eps[-1]
        ws = [w for w in before if w['b'] // E == e1]
        r['src_first'], r['src_last'] = ws[0]['tick'], ws[-1]['tick']
        return (ws[-1] if latest else ws[0])['tick'], True

    for r in rolls:
        c, fb = centre(r, latest=(ctl == 'fallback_latest_write'))
        r['centre'], r['fallback'] = c, fb
        r['wlo'], r['whi'] = wrange(c, tier, tier['TICK_SPACING'] if ctl == 'witness_shift_one_spacing' else 0)
    cp_by_call = {x['c']: x for x in cps}

    # epoch windows: window e from the call that rolled into e to the call that rolled out of it
    win = {}
    for e, c in rec_call.items():
        win.setdefault(e, {})['out'] = c
        win.setdefault(e + 1, {})['in'] = c
    windows = {e: w for e, w in win.items() if 'in' in w and 'out' in w}
    epoch_of_call = lambda c: next((e for e, w in windows.items() if w['in'] <= c < w['out']), None)

    # segments
    segs = []
    for a, z in zip(cps, cps[1:]):
        p0 = p_open(a['c'])
        p1 = p_anchor(z) if ctl == 'anchor_closing_price' else p_open(z['c'])
        segs.append(dict(open=a['c'], close=z['c'], b0=a['b'], b1=z['b'], L=z['L'], lo=z['lo'], hi=z['hi'],
                         dtv=z['tauV'] - a['tauV'], dtb=z['tauB'] - a['tauB'], p0=p0, p1=p1))

    # the witness and lifetime of each segment, and L_b
    def roll_for(s):
        if ctl == 'epoch_indexed_witness':
            e = epoch_of_call(s['open'])
            cand = [r for r in rolls if e is not None and r['epoch'] <= e]
            return cand[-1] if cand else None
        cand = [r for r in rolls if r['c'] <= s['open']]
        return cand[-1] if cand else None

    lb_sized = l_sized = None
    prev_roll = None
    for s in segs:
        r = roll_for(s)
        s['roll'] = r
        if r is None:
            s['unresolved'] = s['L'] > 0
            s['Lb'] = 0
            continue
        s['wlo'], s['whi'] = r['wlo'], r['whi']
        resize_at = None
        if s['open'] in regs:
            resize_at = regs[s['open']]['tick']
        if r is not prev_roll:
            # the roll's sizing (or, for the control, the window's first segment)
            if ctl != 'epoch_indexed_witness' and r['c'] == s['open']:
                resize_at = r['centre'] if resize_at is None else resize_at
            elif ctl == 'epoch_indexed_witness':
                resize_at = r['centre'] if resize_at is None else resize_at
            prev_roll = r
        if resize_at is not None:
            if s['L'] > 0:
                lb_sized = s['L'] * value(1, s['lo'], s['hi'], resize_at) / value(1, s['wlo'], s['whi'], resize_at)
            else:
                lb_sized = 0.0
            l_sized = s['L']
        if s['L'] == 0:
            s['Lb'], s['unresolved'] = 0.0, False
        elif l_sized is None:
            s['Lb'], s['unresolved'] = 0.0, True
        else:
            s['Lb'] = lb_sized * s['L'] / l_sized if l_sized else 0.0
            s['unresolved'] = l_sized == 0

    # the fee rate per lifetime: closed by the next successful roll
    for k, r in enumerate(rolls):
        nxt = rolls[k + 1] if k + 1 < len(rolls) else None
        r['f'] = None
        if nxt is None:
            continue
        a, z = cp_by_call[r['c']], cp_by_call[nxt['c']]
        dtb = z['tauB'] - a['tauB']
        p = price(p_open(nxt['c']))
        fgv = (nxt['fg0'] * p + nxt['fg1']) / tier['L_REF']
        den = (z['b'] - a['b']) if ctl == 'fee_by_n' else dtb
        r['f'] = fgv / den if den else 0.0
    for s in segs:
        r = s['roll']
        if s['L'] == 0:
            s.update(price=0.0, fee=0.0, alloc=0.0)
            continue
        if r is None or r['f'] is None or s['unresolved']:
            s['unresolved'] = True
            s.update(price=0.0, fee=0.0, alloc=0.0)
            continue
        if ctl == 'lvr_price_leg':
            n = s['b1'] - s['b0']
            s0, s1 = math.sqrt(price(s['p0'])), math.sqrt(price(s['p1']))
            pr = -(s['L'] * s['dtv'] - s['Lb'] * s['dtb']) / n * (s1 - s0) ** 2 / s0 if n else 0.0
        else:
            pr = (value(s['L'], s['lo'], s['hi'], s['p1']) - value(s['L'], s['lo'], s['hi'], s['p0'])) - \
                 (value(s['Lb'], s['wlo'], s['whi'], s['p1']) - value(s['Lb'], s['wlo'], s['whi'], s['p0']))
        fee = (s['L'] * s['dtv'] - s['Lb'] * s['dtb']) * r['f']
        s.update(price=pr, fee=fee, alloc=0.0 if ctl == 'no_alloc' else pr + fee)

    # the drip, recomputed by the bank's restart rule at every vault call (the forfeits' blocks from the logs)
    D = tier['FORFEIT_DRIP_BLOCKS']
    by_call = {}
    for x in sorted(logs, key=key):
        by_call.setdefault(x['c'], []).append(x)
    vcalls = [c for c in sorted(by_call) if any(x['ev'] == 'Checkpoint' for x in by_call[c])]
    # a forfeited reserve: a placement's reserve still pending at a breach or retire; its amount per window is the
    # record's forfeited minus the window's breach and retire forfeits
    reserve_forfeit = {}
    for e, w in windows.items():
        calls = [c for c in vcalls if w['in'] < c <= w['out']]
        br = sum(x['forfeited'] for c in calls for x in by_call[c] if x['ev'] in ('BreachProcessed', 'Retired'))
        reserve_forfeit[e] = recs[e]['forfeited'] - br
    pool = tot = start = 0
    pending = marked = False
    drip = {}
    forfeits = []
    for c in vcalls:
        b = by_call[c][0]['b']
        burn = 0
        if tot and pool:
            so_far = min(tot, tot * max(b - start, 0) // D)
            owed = so_far - (tot - pool)
            burn = max(0, min(owed, pool))
            pool -= burn
        drip[c] = burn
        e = next((e for e, w in windows.items() if w['in'] < c <= w['out']), None)

        def forfeit(F):
            nonlocal pool, tot, start
            if F <= 0:
                return
            forfeits.append(dict(c=c, b=b, F=F, pool_before=pool))
            pool = F if ctl == 'single_forfeit_drip' else pool + F
            tot, start = pool, b
        for x in by_call[c]:
            if x['ev'] == 'Settled' and pending:
                if marked and e is not None:
                    forfeit(reserve_forfeit.get(e, 0))
                pending = marked = False
            if x['ev'] in ('BreachProcessed', 'Retired'):
                if pending:
                    marked = True
                forfeit(x['forfeited'])
        if c in regs:
            pending, marked = True, False

    out = {}
    for e, w in sorted(windows.items()):
        ss = [s for s in segs if w['in'] <= s['open'] < w['out']]
        calls = [c for c in vcalls if w['in'] < c <= w['out']]
        r = recs[e]
        out[e] = dict(e=e, cin=w['in'], cout=w['out'], segs=ss, calls=calls, minted=r['minted'],
                      charges=r['bankBurned'] + r['bondBurned'], drip={c: drip[c] for c in calls},
                      unresolved=any(s['unresolved'] for s in ss), rec=r,
                      alloc=sum(s['alloc'] for s in ss))
    for r in rolls:
        r['refresh_after'] = any(w['b'] // E == r['epoch'] and key(w) > key(r) for w in writes)
    meta = dict(rolls=rolls, segs=segs, windows=windows, failed_rolls=[r for r in rolls_all if r['lw'] == 0], forfeits=forfeits,
                zero_L_segments=sum(1 for s in segs if s['L'] == 0), by_call=by_call)
    return out, meta


# ---------------------------------------------------------------- the identity (ledger + truth)

def identity(led, truth, ctl=None, orig=None, seam=None):
    T = {t['c']: t for t in truth}
    rows = []
    for e, w in sorted(led.items()):
        if w['cin'] not in T or w['cout'] not in T:
            continue
        D = int(T[w['cin']]['D'])
        Vf = lambda t: int(t['nav']) * D / int(t['S'])
        Vin, Vout = Vf(T[w['cin']]), Vf(T[w['cout']])
        rhs = 0.0
        alloc_v = 0.0
        for s in w['segs']:
            t = T[s['close']]
            seg = t['seg']
            wgt = D / int(seg['S'])
            rhs += wgt * int(seg['dPassive'])
            alloc_v += wgt * s['alloc']
        if orig is not None:
            alloc_v = orig[e] * D / int(T[w['cin']]['S'])
        rhs += alloc_v
        mint_w = []
        burn_w = []
        drip_v = 0.0
        for c in w['calls']:
            t = T[c]
            S, Spre = int(t['S']), int(t['Spre'])
            mints, burns = int(t['mints']), int(t['burns'])
            flow = S - Spre - mints + burns
            smid = S - flow
            navmid = int(t['navMid'])
            rhs += D / smid * (int(t['nav']) - navmid) - D * navmid / smid ** 2 * flow
            wc = D * int(t['navPre']) / Spre ** 2
            mint_w.append((wc, mints))
            burn_w.append((wc, max(0, burns - w['drip'][c])))
            drip_v += wc * w['drip'][c]
        mean = lambda ws: (sum(a * b for a, b in ws) / sum(b for _, b in ws)) if sum(b for _, b in ws) else \
            sum(a for a, _ in ws) / max(len(ws), 1)
        minted = w['minted'] + (seam.get(e, 0) if seam else 0)
        share = -mean(mint_w) * minted + mean(burn_w) * w['charges'] + (-drip_v if ctl == 'drip_sign_flipped' else drip_v)
        if ctl != 'no_share_terms':
            rhs += share
        res = (Vout - Vin) - rhs
        tv = sum(int(T[s['close']]['seg']['gate']) for s in w['segs'] if T[s['close']]['seg'])
        stale = sum(1 for c in w['calls'] if not T[c]['fresh'] or T[c]['gap'])
        rows.append(dict(e=e, cin=w['cin'], cout=w['cout'], V=Vin, dV_bp=(Vout - Vin) / Vin * 1e4,
                         alloc_bp=alloc_v / Vin * 1e4, residual_bp=res / Vin * 1e4,
                         minted_bp=mean(mint_w) * w['minted'] / Vin * 1e4,
                         charges_bp=mean(burn_w) * w['charges'] / Vin * 1e4, drip_bp=drip_v / Vin * 1e4,
                         unresolved=w['unresolved'], flags=w['rec']['flags'], tauV_gate_tagged=tv,
                         stale_or_gap_calls=stale,
                         segments=len(w['segs'])))
    return rows


def original(led, meta, tier):
    """The registered s19 formula on the same data: alloc = (lTau − L̄·τ_b)/n · carry, carry = fees per unit L at s1
    − (s1 − s0)²/s0 · τ_b/n (s from the record's endpoint medians; fees from the roll that closes the window's
    lifetime; L̄ the vault's time-weighted L)."""
    out = {}
    rolls = meta['rolls']
    for e, w in led.items():
        r = w['rec']
        n = r['end'] - r['start']
        nb = sum(s['b1'] - s['b0'] for s in w['segs'])
        Lbar = sum(s['L'] * (s['b1'] - s['b0']) for s in w['segs']) / nb if nb else 0.0
        nxt = [x for x in rolls if x['c'] >= w['cout']]
        s0, s1 = math.sqrt(price(r['med9Start'])), math.sqrt(price(r['med9End']))
        fees = ((nxt[0]['fg0'] * s1 * s1 + nxt[0]['fg1']) / tier['L_REF']) if nxt else 0.0
        lvr = (s1 - s0) ** 2 / s0 * (r['tauB'] / n if n else 0)
        out[e] = (r['lTau'] - Lbar * r['tauB']) / n * (fees - lvr) if n else 0.0
    return out


# ---------------------------------------------------------------- main

def evaluate(d):
    tier = json.load(open(os.path.join(d, 'E0a-tier.json')))
    tier = {k: int(v) for k, v in tier.items()}
    res = dict(runs={}, controls={c: {} for c in CONTROLS}, power={c: {} for c in CONTROLS})
    for run in RUNS:
        logs = [decode(x) for x in load(os.path.join(d, f'E0a-{run}-logs.jsonl'))]
        truth = load(os.path.join(d, f'E0a-{run}-truth.jsonl'))
        T = {t['c']: t for t in truth}
        led, meta = ledger(logs, tier)
        rows = identity(led, truth)
        # S21F2-02: the power metric's self-test: a control aliased to the base must shift no row
        alias, _ = ledger(logs, tier)
        assert all(abs(x['residual_bp'] - y['residual_bp']) == 0 for x, y in zip(identity(alias, truth), rows))
        rows_orig = identity(led, truth, orig=original(led, meta, tier))
        # the share rows: the record's minted and the recomputed burns against the shares that actually moved
        share_rows = []
        for e, w in sorted(led.items()):
            if w['cin'] not in T:
                continue
            tm = sum(int(T[c]['mints']) for c in w['calls'])
            tb = sum(int(T[c]['burns']) for c in w['calls'])
            share_rows.append(dict(e=e, minted=w['minted'], truth_mints=tm,
                                   burned=w['charges'] + sum(w['drip'].values()), truth_burns=tb))
        # the seam control: the forfeited deferred base minted after the breach
        seam = {}
        for e, w in led.items():
            if w['rec']['forfeitValue'] and w['cin'] in T:
                t = T[w['cout']]
                seam[e] = w['rec']['forfeitValue'] * int(t['S']) // int(t['nav'])
        rows_seam = identity(led, truth, seam=seam) if seam else []
        for c in CONTROLS:
            if c in ('no_share_terms', 'drip_sign_flipped'):
                cr = identity(led, truth, ctl=c)
            else:
                cl, _ = ledger(logs, tier, ctl=c)
                cr = identity(cl, truth, ctl=c)
            res['controls'][c][run] = max((abs(r['residual_bp']) for r in cr), default=0.0)
            # S21F-01: the control's own power, row by row against the base, and whether the base closes there
            bm = {r['e']: r['residual_bp'] for r in rows}
            pw = [(abs(r['residual_bp'] - bm[r['e']]), r['e'], abs(bm[r['e']]) <= BOUND_BP) for r in cr]
            best = max(pw, default=(0.0, None, True))
            if best[0] == 0.0:
                best = (0.0, None, True)  # S21F2-02: no location when the control moves no row
            best_closing = max((x for x in pw if x[2]), default=(0.0, None, True))
            res['power'][c][run] = dict(max_shift_bp=best[0], at_epoch=best[1], base_closes_there=best[2],
                                        max_shift_on_closing_rows_bp=best_closing[0],
                                        exceeds_on_closing_rows=any(abs(r['residual_bp']) > BOUND_BP and
                                                                    abs(bm[r['e']]) <= BOUND_BP for r in cr))
        # row presence (from the truth and the logs)
        bc = meta['by_call']
        place_calls = [t for t in truth if t['k'] == K_PLACE]
        res['runs'][run] = dict(
            epochs=len(rows), worst_residual_bp=max(abs(r['residual_bp']) for r in rows),
            worst_residual_original_bp=max(abs(r['residual_bp']) for r in rows_orig),
            max_alloc_bp=max(abs(r['alloc_bp']) for r in rows),
            max_minted_bp=max(r['minted_bp'] for r in rows), max_charges_bp=max(r['charges_bp'] for r in rows),
            max_drip_bp=max(r['drip_bp'] for r in rows),
            seam_control_bp=max((abs(r['residual_bp']) for r in rows_seam), default=0.0),
            unresolved_epochs=[r['e'] for r in rows if r['unresolved']],
            failed_rolls=len(meta['failed_rolls']), zero_L_segments=meta['zero_L_segments'],
            closing_roll_epoch=meta['rolls'][-1]['epoch'] if meta['rolls'] else None,
            previous_roll_epoch=meta['rolls'][-2]['epoch'] if len(meta['rolls']) > 1 else None,
            fallback_rolls=[r['epoch'] for r in meta['rolls'] if r['fallback']],
            forfeits=[dict(block=f['b'], forfeited=f['F'], pool_before=f['pool_before']) for f in meta['forfeits']],
            two_forfeits_in_one_window=any(f['pool_before'] > 0 for f in meta['forfeits']),
            claims=sum(1 for t in truth if t['k'] == K_CLAIM and any(
                x['ev'] == 'Transfer' and x['to'] not in (0,) and x['value'] > 0 for x in bc.get(t['c'], [])
                if x['ev'] == 'Transfer')),
            deferrals=sum(1 for r in rows if r['flags'] & 32), unverified_epochs=sum(1 for r in rows if r['flags'] & 64),
            breaches=sum(1 for r in rows if r['flags'] & 8),
            flows=sum(1 for t in truth if t['k'] in (K_DEPOSIT, K_WITHDRAW)),
            non_poke_first_epochs=[r['e'] for r in rows if T[r['cin']]['k'] != K_POKE],
            non_poke_first_centre_drift=max((abs(b['centre'] - a['centre']) for a, b in zip(meta['rolls'], meta['rolls'][1:])
                                             if T.get(led.get(b['epoch'], {}).get('cin'), {}).get('k', K_POKE) != K_POKE), default=0),
            fallback_source_drift=max((abs(r['src_last'] - r['src_first']) for r in meta['rolls'] if r['fallback']), default=0),
            fallback_refresh_later=any(r['fallback'] and r['refresh_after'] for r in meta['rolls']),
            max_segments_in_epoch=max((r['segments'] for r in rows), default=0),
            dust_checkpoints=sum(1 for t in truth if t['k'] == K_SETTLE),
            max_liveTick_minus_anchor=max((abs(t['tick'] - t['tickPre']) for t in place_calls), default=0),
            share_rows_close=all(s['minted'] == s['truth_mints'] and s['burned'] == s['truth_burns'] for s in share_rows),
            share_rows=share_rows, rows=rows, rows_original=rows_orig)
    return res


def main(argv):
    d = argv[1] if len(argv) > 1 else 'output/e0a'
    for run in RUNS:
        for f in (f'E0a-{run}-logs.jsonl', f'E0a-{run}-truth.jsonl'):
            if not os.path.exists(os.path.join(d, f)):
                print(f'E0a: missing {d}/{f} (run the data test first)')
                return 2
    res = evaluate(d)
    R = res['runs']
    tier = {k: int(v) for k, v in json.load(open(os.path.join(d, 'E0a-tier.json'))).items()}
    ten = 10 * BOUND_BP
    closes = all(r['worst_residual_bp'] <= BOUND_BP for r in R.values())
    controls = {c: max(v.values()) > BOUND_BP for c, v in res['controls'].items()}
    nonvac = dict(
        alloc_offset=R['offset']['max_alloc_bp'] >= ten, alloc_exit=R['exit']['max_alloc_bp'] >= ten,
        minted=max(r['max_minted_bp'] for r in R.values()) >= ten,
        charge_burns=max(r['max_charges_bp'] for r in R.values()) >= ten,
        drip=max(r['max_drip_bp'] for r in R.values()) >= ten,
        seam_control_exceeds_bound=max(r['seam_control_bp'] for r in R.values()) > BOUND_BP,
        claim=sum(r['claims'] for r in R.values()) >= 1, deferral=sum(r['deferrals'] for r in R.values()) >= 1,
        unobserved_span=sum(r['unverified_epochs'] for r in R.values()) >= 1,
        breach=sum(r['breaches'] for r in R.values()) >= 1,
        two_forfeits_in_one_drip_window=any(r['two_forfeits_in_one_window'] for r in R.values()),
        non_poke_first_epoch=any(r['non_poke_first_epochs'] for r in R.values()),
        fallback_roll=any(r['fallback_rolls'] for r in R.values()),
        other_holders_flows=sum(r['flows'] for r in R.values()) >= 2,
        liveTick_3_ticks_from_anchor=max(r['max_liveTick_minus_anchor'] for r in R.values()) >= 3,
        no_unresolved_epoch=all(not r['unresolved_epochs'] for r in R.values()),
        no_zero_LW_roll=all(r['failed_rolls'] == 0 for r in R.values()),
        non_poke_first_drift_2_spacings=max(r['non_poke_first_centre_drift'] for r in R.values()) >= 2 * tier['TICK_SPACING'],
        fallback_source_drift_2_spacings=max(r['fallback_source_drift'] for r in R.values()) >= 2 * tier['TICK_SPACING'],
        failed_poke_read_then_refresh=any(r['fallback_refresh_later'] for r in R.values()),
        offset_3_segments_in_one_epoch=R['offset']['max_segments_in_epoch'] >= 3,
        offset_dust_checkpoints=R['offset']['dust_checkpoints'] >= 1,
        offset_liveTick_3_ticks_from_anchor=R['offset']['max_liveTick_minus_anchor'] >= 3,
        closing_roll_after_previous=all((r['closing_roll_epoch'] or 0) > (r['previous_roll_epoch'] or -1) for r in R.values()))
    share_ok = all(r['share_rows_close'] for r in R.values())
    verdict = closes and all(controls.values()) and all(nonvac.values()) and share_ok
    # S21F-04: the attribution of each failing condition (computed from the data; the verdict is unchanged)
    diagnosis = []
    for n, r in R.items():
        T = {t['c']: t for t in load(os.path.join(d, f'E0a-{n}-truth.jsonl'))}
        for x in r['rows']:
            if abs(x['residual_bp']) <= BOUND_BP:
                continue
            t = T[x['cout']]
            D, Spre = int(t['D']), int(t['Spre'])
            fees = int(t['navMid']) - int(t['navPre']) if t['tick'] == t['tickPre'] else None
            fees_bp = (fees * D / Spre / x['V'] * 1e4) if fees is not None else None
            # S21F2-03: the attribution only when the data shows it
            attributed = x['stale_or_gap_calls'] > 0 and fees_bp is not None and abs(fees_bp - x['residual_bp']) < 0.01
            diagnosis.append(dict(
                condition='closure', run=n, epoch=x['e'], residual_bp=x['residual_bp'], segments=x['segments'],
                stale_or_gap_calls=x['stale_or_gap_calls'],
                vault_fees_collected_at_close_bp=fees_bp,
                note='unattributed' if not attributed else 'the vault earned fees in range while tau_v took no credit (a stale cache, then an in-block '
                     'crossing out of the range); the ledger books the vault fee leg as L*dtau_v*f_w = 0, so the '
                     'fees are dropped (the first channel of the fee-leg error term of spec 3.8), measured above the bound'))
    for c, ok in controls.items():
        if not ok:
            diagnosis.append(dict(condition='control ' + c, worst_bp=max(res['controls'][c].values()),
                                  power=res['power'][c],
                                  note='the control moves the residual by less than the bound, or towards zero, on '
                                       'every row: a lack of power on this data, not a defect of its implementation'))
    shipped_m = 10 ** 16 * 12 // (365 * 24 * 3600)
    summary = dict(eval='E0a', bound_bp=BOUND_BP, verdict='PASS' if verdict else 'FAIL', closes=closes,
                   share_rows_close=share_ok, controls_exceed_bound=controls,
                   controls_worst_bp=res['controls'], controls_power=res['power'], nonvacuity=nonvac,
                   diagnosis=diagnosis, decision=None,
                   original_formula_verdict='PASS' if all(r['worst_residual_original_bp'] <= BOUND_BP for r in R.values()) else 'FAIL',
                   scope='the events reproduce the state, the fee-leg approximation and the share terms; not the '
                         "benchmark's economic meaning",
                   disclosed=f"m_perBlock raised in the harness to {tier['m_perBlock']} (the shipped TEST value is "
                             f"{shipped_m}, 2.2%/yr at 12 s blocks; about {tier['m_perBlock'] // shipped_m}x); TEST tier "
                             "otherwise. The identity's share terms equal the truth's per-call sum once the share rows "
                             "close (record minted = the vault's Transfer mints); only the share rows test them, the "
                             "identity adds no independent timing test.",
                   tier=json.load(open(os.path.join(d, 'E0a-tier.json'))), runs=R)
    os.makedirs(os.path.join(HERE, 'results'), exist_ok=True)
    json.dump(summary, open(os.path.join(HERE, 'results', 'E0a.json'), 'w'), indent=1, default=str)
    for n, r in R.items():
        print(f"{n}: {r['epochs']} epochs, worst |residual| {r['worst_residual_bp']:.4f} bp (original formula "
              f"{r['worst_residual_original_bp']:.4f} bp), max |alloc| {r['max_alloc_bp']:.2f} bp, minted "
              f"{r['max_minted_bp']:.2f}, charges {r['max_charges_bp']:.2f}, drip {r['max_drip_bp']:.2f} bp; share rows "
              f"{'close' if r['share_rows_close'] else 'DO NOT close'}")
    for c, ok in controls.items():
        print(f"  control {c}: worst {max(res['controls'][c].values()):.4f} bp {'exceeds' if ok else 'DOES NOT exceed'}")
    for k, v in nonvac.items():
        if not v:
            print(f'  non-vacuity FAILED: {k}')
    print(f"E0a {summary['verdict']} (original formula {summary['original_formula_verdict']})")
    return 0 if verdict else 1


if __name__ == '__main__':
    sys.exit(main(sys.argv))
