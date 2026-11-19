#!/usr/bin/env python3
"""E8 (spec §10): the reported operating numbers. Verdict REPORTED: every item of the row is present, each with a
value (and its source and data kind) or `not_run` with the reason. Assembled from existing measurements (P-18); no
new simulation. Data kinds: `gbm-synthetic` (U-IC2's held-out paths, seeds 700-1099), `fork` (chain 1 at block
26443420), `closed-form`, `arithmetic` (computed from measured numbers, not measured itself).

  python3 eval/e8.py [--out eval/results/E8.json]
"""
import hashlib, json, os, re, subprocess, sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DAYS, NAV_ETH = 180, 1000.0
CURE_BLOCKS, BLOCKTIME = 7200, 12      # mainnet tier (script/Tiers.sol)
MAX_SWAP_EPOCH = 0.05
KREF_LOG = 'eval/supplements/E8/kref-fork-26443420.log'


def sha(p):
    return hashlib.sha256(open(os.path.join(ROOT, p), 'rb').read()).hexdigest()


def git(*a, cwd=ROOT):
    return subprocess.run(['git', *a], cwd=cwd, capture_output=True, text=True).stdout.strip()


def kref():
    """The K-ref rows (push t ticks; one-block hold; round trip; notional), in WETH."""
    nums = [ln.strip() for ln in open(os.path.join(ROOT, KREF_LOG)) if re.fullmatch(r'\s*\d+\s*', ln)]
    out = []
    i = nums.index('201555') + 1 if '201555' in nums else 0
    while i + 3 < len(nums) + 0 and len(out) < 4:
        t, hold, rt, notional = int(nums[i]), int(nums[i + 1]) / 1e18, int(nums[i + 2]) / 1e18, int(nums[i + 3]) / 1e18
        out.append(dict(push_ticks=t, one_block_hold_weth=round(hold, 4), round_trip_weth=round(rt, 4),
                        notional_weth=round(notional, 1)))
        i += 4
    return out


def main(argv):
    out = argv[argv.index('--out') + 1] if '--out' in argv else None
    u = json.load(open(os.path.join(ROOT, 'eval', 'results', 'U-IC2.json')))
    agg = json.load(open(os.path.join(ROOT, 'eval', 'supplements', 'U-IC2-agg.json')))
    rb = json.load(open(os.path.join(ROOT, 'eval', 'supplements', 'E8', 'refband.json')))
    rows = {(r['bucket'], r['bot']): r for r in agg['rows'] if r['m'] == 0.022 and not r['observer']}
    buckets = list(u['buckets'])
    l8, nv, hc = u['l3_form']['l8'], u['nonvacuity'], u['l3_form']['honest_cost']
    yr = 365.0 / DAYS
    k = kref()
    gs8 = ('gbm-synthetic (U-IC2 held-out seeds 700-1099, mimic edge-12h, L8 form: m 1%/yr, an eroding bond, the gate '
           'enforced, no observer)')
    gs = 'gbm-synthetic (U-IC2 held-out seeds 700-1099, mimic edge-12h, m 2.2%/yr, no observer, NAV 1000 ETH, gas 5 gwei)'
    items = {
        'bond_exhaustion_sigma_120': dict(
            value={'actions': l8['gbm-120']['actions'], 'reverts': l8['gbm-120']['reverts'],
                   'breached_paths': l8['gbm-120']['breached'], 'fallbacks': l8['gbm-120']['fallbacks']},
            source='U-IC2 l3_form.l8 (eroding BOND_RATIO bond, solvency gate enforced, m 1%/yr)', data=gs8),
        'liveness_share_at_shipped_bands': dict(
            value={b: 1.0 - l8[b]['blocked_share'] for b in buckets}, source='U-IC2 l3_form.l8 blocked_share', data=gs8),
        'suspect_segment_share': dict(not_run='the suspect flag belongs to the allocation ledger (claim withdrawn, H); '
                                              'the simulation does not compute it'),
        'fence_blocking_share_REF_BAND_60': dict(
            value={kk: rb[kk]['60']['conservative'] for kk in rb if kk.startswith('test-')},
            source='refband cell (30-day paths, 1-minute steps, held-out "test" keys), conservative column, REF_BAND 60',
            data='gbm-synthetic and t3 (fat tails)'),
        'reference_charge_distribution': dict(
            value={b: {'charges_per_path_eth': round(rows[(b, 'edge-12h')]['charges_mean'], 4),
                       'charges_bps_of_nav_per_year': round(rows[(b, 'edge-12h')]['charges_mean'] / NAV_ETH * 1e4 * yr, 2),
                       'actions_per_path': round(rows[(b, 'edge-12h')]['actions_mean'], 1),
                       'settlement_extra_mean_frac_nav': hc[b]['all']['x_fa_mean'],
                       'settlement_extra_max_frac_nav': hc[b]['all']['x_fa_max']} for b in buckets},
            source='U-IC2 per-path rows (eval/supplements/U-IC2-agg.json) and l3_form.honest_cost', data=gs),
        'lag_leak_per_sigma': dict(
            value={'sigma_60_pct_nav_per_year': 0.0, 'sigma_120_pct_nav_per_year': 0.0,
                   'bounds_pct_nav_per_year': [0.1, 0.6], 'cycles_per_sigma': 600},
            source='row L1 (model/cells/m2d_rate.py) on held-out seeds 300-499, an earlier configuration (before EDGE_DELAY '
                   '12 GRID); the source record is not public, the value is copied',
            data='gbm-synthetic'),
        'swap_cap_binding_frequency': dict(
            value={b: {'mimic_SwapCap_reverts': rows[(b, 'edge-12h')]['reverts'].get('SwapCap', 0),
                       'mimic_actions_per_path': round(rows[(b, 'edge-12h')]['actions_mean'], 1),
                       'l8_reverts': l8[b]['reverts']} for b in buckets},
            source='U-IC2 per-path rows (m 2.2%/yr) and l3_form.l8 (the l8_reverts column: m 1%/yr, eroding bond); mimic only',
            data=gs),
        'p99_actions_per_epoch_per_tier': dict(
            not_run='the simulation keeps per-path totals, not per-epoch series',
            per_path_mean_actions_per_epoch={b: round(rows[(b, 'edge-12h')]['actions_mean'] / (DAYS * 86400 / BLOCKTIME
                                                                                                / 50_400), 2)
                                             for b in buckets}),
        'forfeiture_manipulation_cost_kref': dict(
            value={'per_block_measured': k,
                   'hold_24h_competitive_bound_weth': {r['push_ticks']: round(r['one_block_hold_weth'] * CURE_BLOCKS, 1)
                                                       for r in k},
                   'hold_10min_twap_competitive_bound_weth': {r['push_ticks']: round(r['one_block_hold_weth'] * 50, 1)
                                                              for r in k},
                   'single_builder_bound': 'about one round trip per push (the round_trip_weth column), if one builder '
                                           'wins every block of the hold and censors arbitrage on the reference pool '
                                           '(outside the model, spec §5.1 MK-1)'},
            source=f'test_K_ref__deepPoolPushCostPerTick on the fork at block 26443420 ({KREF_LOG})',
            data='fork (per block, measured); holds = arithmetic',
            note='an edge-placed vault keeps the reference near one edge, so a push of about 10 ticks can move its '
                 'TWAP out of range; forcing a breach needs CURE_BLOCKS (7,200 blocks, 24 h) of observed time out'),
        'cheapest_legal_clock_reset': dict(
            value={b: {'breach_reset_charges_per_path_eth': round(rows[(b, 'breach-reset')]['charges_mean'], 4),
                       'breach_reset_actions_per_path': round(rows[(b, 'breach-reset')]['actions_mean'], 2),
                       'charge_per_reset_eth': round(rows[(b, 'breach-reset')]['charges_mean'] /
                                                     max(rows[(b, 'breach-reset')]['actions_mean'], 1e-9), 5),
                       'gas_per_placement_eth': 450_000 * 5e-9} for b in buckets},
            source='U-IC2 per-path rows, bot breach-reset (one real placement just before CURE)', data=gs),
        'curator_recapture_share_of_burns': dict(not_run='needs the curator-held share at each burn; not instrumented'),
        'time_to_feasibility_one_sided_inventory': dict(not_run='no cell measures it (R2d-10)'),
        'calendar_window_epoch_swap_cap': dict(
            value={'max_swapped_frac_nav_across_a_boundary': 2 * MAX_SWAP_EPOCH},
            source='closed form: MAX_SWAP_EPOCH (5% of NAV) per epoch, twice across one epoch boundary',
            data='closed-form'),
        'rejected_design_numbers': dict(
            value={'skill_reimbursement_adv3_multiple_of_base': 'about 10x base (ADV-3 under a phi-scaled charge)',
                   'carry_benchmark_random_curator_pay_above_base_pct': 'see E9: +40 / +6 / -13',
                   'idle_curator_under_phi_10pct_x_base': [-37.6, 3.4]},
            source='E9 and the ADV-3 phi-scaled control', data='design-phase benchmark simulator'),
        'keepalive_pings_per_year_and_cost': dict(
            value={b: {'pings_per_path_year': round(nv[b]['edge-12h']['pings'] / nv[b]['edge-12h']['paths'] * yr, 1),
                       'cost_eth_per_year': round(nv[b]['edge-12h']['pings'] / nv[b]['edge-12h']['paths'] * yr
                                                  * 100_000 * 5e-9, 4)} for b in buckets},
            source='U-IC2 nonvacuity (pings), 100,000 gas per ping at 5 gwei (model/cells/sim.py)', data=gs),
        'gate_closed_share': dict(not_run='the simulation does not count vest-gate closures'),
        'depositor_observer_break_even': dict(not_run='no closed form was registered before this run; the observer '
                                                      'environment numbers are in eval/supplements/U-IC2-observer.json'),
    }
    n_nr = sum(1 for v in items.values() if 'not_run' in v)
    res = {
        'id': 'E8', 'question': 'operating numbers (spec §10 row E8, itemised)', 'pass_rule': 'reported',
        'verdict': 'REPORTED', 'items': items, 'not_run_count': n_nr,
        'evidence': {'eval/results/U-IC2.json': sha('eval/results/U-IC2.json'), 'u_ic2_parts': agg['parts'],
                     KREF_LOG: sha(KREF_LOG), 'eval/supplements/E8/refband.json': sha('eval/supplements/E8/refband.json'),
                     'eval/e8.py': sha('eval/e8.py')},
        'limits': 'the U-IC2 per-path parts (two files, 670 MB) are not in the repository; their sha256 is recorded and eval/supplements/U-IC2-agg.json holds the aggregates used here; ' + 'GBM items are synthetic paths with third-party depth 0 and gas at 5 gwei (a placeholder, not the '
                  "replay window's p50); the lag leak is from the s17 configuration (before EDGE_DELAY 12 GRID); no item "
                  'is from a historical replay',
        'commit_public': git('rev-parse', 'HEAD'),
        'commit_internal': git('rev-parse', 'HEAD', cwd=os.path.join(ROOT, 'internal')),
    }
    t = json.dumps(res, indent=1, allow_nan=False) + '\n'
    if out:
        open(os.path.join(ROOT, out), 'w').write(t)
    print(t)
    return 0


if __name__ == '__main__':
    sys.exit(main(sys.argv[1:]))
