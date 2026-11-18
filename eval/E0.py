#!/usr/bin/env python3
"""E0 (spec §3.8, §10), REDUCED form (registry F-049): the NAV identity of the allocation ledger against a passive leg
of the vault's own L on the mandate range, <= 0.3 bp of NAV per epoch. See "Scope of this run" below.

Reads the per-epoch data of `test/eval/E0Data.t.sol` (one file per run: the vault's NAV at the reference after the
curator's action and at the end of the epoch, the action's loss, the passive leg's change, the epoch record and the
witness's per-unit fees) and checks, per epoch,

    NAV_end − NAV_after  =  ΔNAV_passive + alloc + residual        (the action's loss is before the window)
    alloc = (lTau − L·τ_b)/n · carry
    carry = fees per unit L over the window, valued at s1  −  (s1 − s0)²/s0 · τ_b/n     (s: the endpoint medians' sqrt)

with |residual| <= 0.3 bp of NAV_after. (The full form's fixed-depositor share terms are not evaluated here; mints
and burns move shares, not NAV, and U-record's closure pins the share side.)

  TS_E0_OUT=output/e0 forge test --mc E0DataTest && python3 eval/E0.py output/e0      # writes eval/results/E0.json

Scope of this run (s19 fix review S19F-04): a REDUCED form of spec §3.8's E0. It checks the NAV identity over the
window after the curator's action, on a passive leg with the vault's own L (equal L, not equal value) and without
the fixed-depositor share terms, the claim/deferral/breach/drip rows or the non-poke first checkpoint. Its same-range
run is a tautology (the passive leg is the vault's position on the same range: 0.0000 bp is rounding) and its
control is vacuous (alloc = 0 in both runs). What it does show: an offset curator whose range and the mandate range
both contain the path differs by a term §3.8 does not carry, the inventory term (x0_v − x0_b)(p1 − p0), exact here
because an in-range position's second derivative does not depend on its bounds. The FAIL is therefore reported, not
the pass/fail of the full form, which the inventory gap would also fail (registry F-049; the full form needs its own
review round before it runs).

This result stays published as a negative result. The amended ledger (an exact per-segment price leg at the
reference, and a fee leg per witness lifetime; spec §3.8, amended after this result) is evaluated as E0a
(`eval/E0a.py`), which also reports this formula on the same data. `script/claims.py` counts E0 as met only through a
PASS of E0a, never as a pass of its own.
"""
import json, math, os, sys

BOUND_BP = 0.3
Q96 = 2 ** 96
HERE = os.path.dirname(os.path.abspath(__file__))


def sqrt_at(tick):
    return math.sqrt(1.0001 ** tick)


def epoch_rows(eps):
    rows = []
    for x in eps:
        n = x['end'] - x['start']
        nav0, nav1 = int(x['navAfter']), int(x['navEnd'])
        s0, s1 = sqrt_at(x['med9Start']), sqrt_at(x['med9End'])
        p1 = (int(x['sqrt1']) / Q96) ** 2
        L = int(x['L'])
        lref = 1e18
        fees = (int(x['fees0']) * p1 + int(x['fees1'])) / lref  # per unit L, token1 units
        lvr = (s1 - s0) ** 2 / s0 * (x['tauB'] / n if n else 0)  # per unit L
        carry = fees - lvr
        alloc = (int(x['lTau']) - L * x['tauB']) / n * carry if n else 0.0
        resid = (nav1 - nav0) - int(x['dPassive']) - alloc
        # informational (not part of §3.8): the inventory term the ledger omits, (x0_v - x0_b)·(p1 - p0), with x0 the
        # token0 a unit of L holds on each range at the start price
        inv = 0.0
        if 'lo' in x:
            p0, p1x = (int(x['sqrt0']) / Q96) ** 2, (int(x['sqrt1']) / Q96) ** 2
            x0 = lambda lo, hi: L * max(0.0, 1 / math.sqrt(p0) - 1 / sqrt_at(hi)) if sqrt_at(lo) <= math.sqrt(p0) else L * (1 / sqrt_at(lo) - 1 / sqrt_at(hi))
            inv = (x0(x['lo'], x['hi']) - x0(x['wlo'], x['whi'])) * (p1x - p0)
        rows.append(dict(e=x['e'], n=n, nav=nav0, dVault=nav1 - nav0, dPassive=int(x['dPassive']), alloc=alloc,
                         residual=resid, residual_bp=resid / nav0 * 1e4, inventory_bp=inv / nav0 * 1e4,
                         residual_with_inventory_bp=(resid - inv) / nav0 * 1e4, alloc_bp=alloc / nav0 * 1e4,
                         loss_bp=int(x['loss']) / nav0 * 1e4, charge=int(x['charge']), tauV=x['tauV'], tauB=x['tauB']))
    return rows


def evaluate(runs):
    out = {}
    for name, eps in runs.items():
        rows = epoch_rows(eps)
        worst = max(abs(r['residual_bp']) for r in rows)
        # the positive control: the identity without alloc
        worst_noalloc = max(abs(r['residual_bp'] + r['alloc_bp']) for r in rows)
        out[name] = dict(epochs=len(rows), worst_residual_bp=worst,
                         worst_residual_with_inventory_bp=max(abs(r['residual_with_inventory_bp']) for r in rows), worst_without_alloc_bp=worst_noalloc,
                         max_alloc_bp=max(abs(r['alloc_bp']) for r in rows), charges=sum(r['charge'] for r in rows),
                         passed=worst <= BOUND_BP, rows=rows)
    return out


def main(argv):
    d = argv[1] if len(argv) > 1 else 'output/e0'
    runs = {}
    for n in ('same', 'offset'):
        p = os.path.join(d, f'E0-{n}.json')
        if not os.path.exists(p):
            print(f'E0: missing {p} (run the data test first)'); return 2
        runs[n] = json.load(open(p))
    res = evaluate(runs)
    nonvac = res['offset']['max_alloc_bp'] >= 10 * BOUND_BP and res['offset']['charges'] > 0
    control = res['offset']['worst_without_alloc_bp'] > BOUND_BP
    verdict = all(r['passed'] for r in res.values()) and nonvac and control
    vacuous_control = res['offset']['max_alloc_bp'] == 0
    summary = dict(eval='E0', scope='reduced (registry F-049): equal-L passive leg, no fixed-depositor share terms, '
                   'no claim/deferral/breach/drip rows', bound_bp=BOUND_BP, verdict='PASS' if verdict else 'FAIL',
                   nonvacuous=nonvac, same_range_tautology=True, control_vacuous=vacuous_control,
                   control_exceeds_bound=control, negative_result=True, superseded_by='E0a',
                   decision='published as a negative result; the ledger amended after the data and evaluated as E0a (2026-11-18)',
                   note='registered form failed; the ledger was amended after this result (spec §3.8) and is '
                        'evaluated as E0a, with this formula reported next to it', runs=res)
    os.makedirs(os.path.join(HERE, 'results'), exist_ok=True)
    json.dump(summary, open(os.path.join(HERE, 'results', 'E0.json'), 'w'), indent=1)
    for n, r in res.items():
        print(f"{n}: worst |residual| {r['worst_residual_bp']:.4f} bp (with the inventory term "
              f"{r['worst_residual_with_inventory_bp']:.4f} bp), without alloc {r['worst_without_alloc_bp']:.4f} bp, "
              f"max |alloc| {r['max_alloc_bp']:.4f} bp, charges {r['charges']}")
    print(f"E0 {summary['verdict']} (non-vacuous {nonvac}, control exceeds the bound {control})")
    return 0 if verdict else 1


if __name__ == '__main__':
    sys.exit(main(sys.argv))
