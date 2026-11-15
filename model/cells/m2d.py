#!/usr/bin/env python3
"""Cell (f): M-2d, price pushes in a vault-dominated pool (G0 item 1b), plus the in-range lag lane (F-021).

States come from an edge60-managed path (cell (d)'s sampler): "exit" = the first hour out of range (the vault is
out of range, the region between the market and its old edge holds only third-party liquidity), "in" = a random
hour in range. The coalition is the curator plus a Sybil with ordering control; nets are valued at the market
price r; the curator holds the NAV-cap fraction of the supply; the charge is burned in shares (cell (d)'s
accounting, `n4.coalition_net`).

Lanes (pre-registered in G0 item 1b):
1. one-shot (exit states): push d ∈ {20, 100, 1000, 2000, 3000} ticks either way through third-party depth
   k ∈ {0, 0.05, 0.3, 1, 3} × the vault's liquidity on the push path, any legal range at the pushed spot (a 40-tick
   grid plus both edges plus a ±30-tick refinement), revert to r. Rule: max net ≤ 1 wei. Controls: the reference
   guard off (no fence, no containment, spot-only charge) ⇒ ≈ +2.5% NAV at d = 1000; fence-only (fence and
   containment, spot-only charge) ⇒ > 0 at d ≤ REF_BAND.
2. rate (≥ 50 cycles per σ bucket; both states): the reference lags the market by ℓ drawn from the 10-minute TWAP's
   lag on 1-minute GBM paths; per cycle the best net over d ∈ {20, 30}, both directions, k ∈ {0, 1} and the range
   search; the rate = (cycles per year at one per COOLDOWN) × E[max(0, net)], with the out-of-range and in-range
   lanes weighted by edge60's out-of-range and in-range time share (cell (a) fit). Rule: the summed lag leak ≤ 0.1%
   NAV/yr at σ 60% and ≤ 0.6% at σ 120%. Control: fence-only (the reference leg off) ⇒ rate > 0.
3. JIT (exit states): the Sybil pushes spot d ∈ {0, 20, 60} against the curator, adds JIT liquidity J ∈ {1e20,
   1e22, 1e24} on the curator's 10-tick slippage band, the curator's pre-swap of MAX_SWAP_ACTION (2% of NAV), run
   from idle after the old position is removed, buys its scarce token through it, the Sybil removes the JIT
   position and reverts. Rule: coalition net ≤ 1 wei.
   Control: a φ = 10% charge ⇒ > 0.
4. coerced grief (in states): a third party (not colluding) pushes spot d ∈ {20, 40, 60} before an honest
   in-place edge60 placement and reverts after it. Reported: the griefer's net and the curator's extra charge.
"""
import json
import math
import multiprocessing as mp
import os
import random
import sys
import time

import numpy as np

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from common import R, sqrt_at, SIGMAS, TEST_SEEDS, W, SPACING, floor_sp, sample_state, mean_ci, pctl, edge60_range  # noqa
from n4 import run_attack, coalition_net, candidates, F_CAP  # noqa

ETH = 10 ** 18
D1 = (20, 40, 50, 60, 100, 1000, 2000, 3000)   # 20 and 100+ pre-registered; 40–60 added (r1 R1-07)
K1 = (0, 0.05, 0.3, 1, 3)
CYCLES_PER_YEAR = 365 * 24 * 3600 // (30 * 12)     # one per COOLDOWN (30 blocks of 12 s)
OUT_SHARE = {0.40: 1 - 0.970, 0.60: 1 - 0.952, 1.20: 1 - 0.901}   # edge60 out-of-range time (cell (a) fit, f_actual)
# An out-of-range attack re-places the vault, so it is available once per exit; the attacker may pick the best of
# the COOLDOWN slots in the hour before edge60 re-places (10 slots; E[max of n] <= n·E[max(0, ·)]). Exits per year =
# edge60's placements per year (cell (a) fit; hover-triggered placements are counted too).
EXITS_PER_YEAR = {0.40: 179.0, 0.60: 284.0, 1.20: 557.0}   # edge60 placements/yr, cell (a) fit
SLOTS_PER_EXIT = 10
GAS_GWEI = 5
CYCLE_GAS_WEI = (450_000 + 2 * 120_000) * GAS_GWEI * 10 ** 9   # reported variant; the gated net excludes gas
BOUND = {0.40: None, 0.60: 1e-3, 1.20: 6e-3}
OUT = os.path.join(os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))), "output", "g0")


def n_opps(variant, sigma):
    """Opportunities per year: one per COOLDOWN in range; out of range one per exit with up to SLOTS_PER_EXIT slots
    (an attack re-places the vault). Not an upper bound: a curator may wait up to CURE within an exit (r1 R1-06)."""
    if variant == "exit":
        return min(CYCLES_PER_YEAR * OUT_SHARE[sigma], EXITS_PER_YEAR[sigma] * SLOTS_PER_EXIT)
    return CYCLES_PER_YEAR * (1 - OUT_SHARE[sigma])


def third_range(r, d_signed):
    a, b = sorted((r, r + d_signed))
    return floor_sp(a) - 100, floor_sp(b) + 110


def best_attack(h, r, ref, ds, ks, guard, band=60, phis=(R.WAD,), extra=None):
    """Best coalition net (wei) over d, direction, k and range; 0 if no legal attack (the coalition abstains)."""
    best = {phi: (0, None) for phi in phis}
    for d in ds:
        for sg in (1, -1):
            spot_t = r + sg * d
            if guard and abs(spot_t - ref) > band:
                continue
            for k in ks:
                tl, th = third_range(r, sg * d)
                cache = {}
                cand = candidates(r, spot_t, ref if guard else spot_t)
                for lo in cand:
                    cache[lo] = run_attack(h, r, ref, sg * d, lo, k, band, guard=guard, third_lo=tl, third_hi=th,
                                           extra_sqrt=extra)
                legal = [lo for lo in cand if cache.get(lo)]
                if legal:
                    top = max(legal, key=lambda lo: coalition_net(cache[lo], 0, F_CAP))
                    for lo in range(top - 30, top + 40, SPACING):
                        if lo not in cache:
                            cache[lo] = run_attack(h, r, ref, sg * d, lo, k, band, guard=guard, third_lo=tl, third_hi=th,
                                           extra_sqrt=extra)
                # the edge geometry (one-sided inventory): spot one unit inside the inventory-side edge at r ± d
                e = floor_sp(r + sg * d)
                edges = {("e0", e): (e, sqrt_at(e) + 1), ("e1", e): (e + SPACING - W, sqrt_at(e + SPACING) - 1)}
                for key, (lo_e, ps) in edges.items():
                    cache[key] = run_attack(h, r, ref, sg * d, lo_e, k, band, push_sqrt=ps, guard=guard,
                                            third_lo=tl, third_hi=th, extra_sqrt=extra)
                for lo, a in cache.items():
                    if a:
                        for phi in phis:
                            n = coalition_net(a, phi, F_CAP)
                            if n > best[phi][0]:
                                best[phi] = (n, dict(d=d, sg=sg, k=k, lo=lo, nav=a["nav0"], charge=a["charge"]))
    return best


def lags(seed, sigma, n):
    """n signed lags (spot tick − floored 10-minute TWAP) at random minutes of a 1-day 1-minute GBM path."""
    rng = np.random.default_rng(77_000_003 * seed + int(sigma * 1000))
    sd = sigma * math.sqrt(60 / (365 * 86400)) / math.log(1.0001)
    x = np.cumsum(sd * rng.standard_normal(1440 + 10))
    idx = rng.integers(10, len(x), n)
    out = []
    for i in idx:
        w = x[i - 10:i + 1]
        avg = (w.sum() - 0.5 * (w[0] + w[-1])) / 10
        out.append(int(round(x[i])) - math.floor(avg))
    return out


def jit_lane(h, r, d, J):
    """The curator's 2%-of-NAV rebalanceSwap buying its scarce token through the Sybil's JIT liquidity at a pushed
    spot. Returns (coalition net at φ = 1, at φ = 0.1) in wei, or None if the swap is not legal."""
    sr = sqrt_at(r)
    pool = R.Pool(sr, 500)
    x, y = R.position_amounts(h.L, h.lo, h.hi, sr) if h.L else (0, 0)
    hb = R.Holdings(h.idle0 + x, h.idle1 + y)   # a pre-swap runs from idle after the old position is removed
    nav_r = R.value(hb, sr)[0]
    zfo = R.q(hb.idle0 + R.position_amounts(hb.L, hb.lo, hb.hi, sr)[0], sr) >= hb.idle1 + \
        R.position_amounts(hb.L, hb.lo, hb.hi, sr)[1]   # mostly token0: sell token0 (buy token1)
    sgn = -1 if zfo else 1                                 # the adverse push: lower price when selling token0
    ain, aout, z = pool.push_to(sqrt_at(r + sgn * d))
    syb0, syb1 = (-ain, aout) if z else (aout, -ain)
    tick_spot = pool.tick
    if abs(tick_spot - r) > 60:
        return None
    lim = tick_spot - 10 if zfo else tick_spot + 10
    jlo, jhi = (floor_sp(lim), floor_sp(tick_spot) + SPACING) if zfo else (floor_sp(tick_spot), floor_sp(lim) + SPACING)
    if J:
        j0, j1, _, _ = pool.modify("jit", jlo, jhi, J)
        syb0, syb1 = syb0 - j0, syb1 - j1
    if hb.L:
        _, _, f0, f1 = pool.modify("vault", hb.lo, hb.hi, 0)
        hb.idle0 += f0
        hb.idle1 += f1
    before = hb.copy()
    s_spot = pool.s
    amt = (R.mul_div(nav_r * 2 // 100, 1 << 192, sr * sr) if zfo else nav_r * 2 // 100)
    amt = min(amt, hb.idle0 if zfo else hb.idle1)
    if amt == 0:
        return None
    used, out = pool.swap(zfo, amt, sqrt_at(lim))
    after = hb.copy()
    if zfo:
        after.idle0, after.idle1 = after.idle0 - used, after.idle1 + out
    else:
        after.idle1, after.idle0 = after.idle1 - used, after.idle0 + out
    if after.L:
        _, _, f0, f1 = pool.modify("vault", after.lo, after.hi, 0)
        after.idle0 += f0
        after.idle1 += f1
    charge, p_star, nav_after, burn, ok = R.measure(before, after, s_spot, sqrt_at(r), 10 ** 24, 0, 10 ** 24)
    if J:
        j0, j1, f0, f1 = pool.modify("jit", jlo, jhi, -J)
        syb0, syb1 = syb0 - j0 + f0, syb1 - j1 + f1
    ain, aout, z = pool.push_to(sr)
    syb0, syb1 = (syb0 - ain, syb1 + aout) if z else (syb0 + aout, syb1 - ain)
    if after.L:
        _, _, f0, f1 = pool.modify("vault", after.lo, after.hi, 0)
        after.idle0 += f0
        after.idle1 += f1
    nav1 = R.value(after, sr)[0]
    sybil = (R.q(syb0, sr) if syb0 >= 0 else -R.mul_div_up(-syb0, sr * sr, 1 << 192)) + syb1
    a = dict(sybil=sybil, nav0=nav_r, nav1=nav1, charge=charge, navp=nav_after)
    return coalition_net(a, R.WAD, F_CAP), coalition_net(a, R.WAD // 10, F_CAP), nav_r


def grief_lane(h, r, d):
    """A non-colluding griefer pushes spot d against an honest in-place edge60 placement and reverts after it.
    Returns (griefer net, curator's extra charge) in wei, or None if no legal honest placement exists."""
    out = None
    for sg in (1, -1):
        x0, y0 = R.position_amounts(h.L, h.lo, h.hi, sqrt_at(r)) if h.L else (0, 0)
        inv = R.Holdings(h.idle0 + x0, h.idle1 + y0)
        lo, hi = edge60_range(inv, r, r + sg * d)
        a = run_attack(h, r, r, sg * d, lo, 0, 60)
        b = run_attack(h, r, r, 0, lo, 0, 60)
        if not a or not b:
            # r2 R2-05: when the in-place placement is not legal (a one-sided inventory), the honest curator uses the
            # bundled edge move: its own move to one unit inside the inventory-side edge at the floored reference
            # overrides the griefer's push; the move leg is attributed to the griefer (conservative for the lane)
            c = floor_sp(r)
            mostly0 = R.q(inv.idle0, sqrt_at(r)) >= inv.idle1
            lo, ps = (c, sqrt_at(c) + 1) if mostly0 else (c + SPACING - W, sqrt_at(c + SPACING) - 1)
            a = run_attack(h, r, r, sg * d, lo, 0, 60, push_sqrt=ps)
            b = a
            if not a:
                continue
        g = (a["sybil"], a["charge"] - b["charge"])
        if out is None or g[0] > out[0]:
            out = g
    return out


def run_path(args):
    seed, sigma = args
    rows = []
    rng = random.Random(seed * 101 + int(sigma * 100))
    h, r, _ = sample_state(seed, sigma, exit_state=True)
    # lane 1: one-shot, guard on (the fence blocks d > REF_BAND: the coalition abstains, net 0)
    for d in D1:
        for k in K1:
            on = best_attack(h, r, r, (d,), (k,), True)[R.WAD]
            rows.append(dict(lane="oneshot", seed=seed, sigma=sigma, d=d, k=k, net=on[0],
                             nav=R.value(h, sqrt_at(r))[0], blocked=on[1] is None and d > 60))
        off = best_attack(h, r, r, (d,), (0,), False)[R.WAD]
        rows.append(dict(lane="oneshot-ctl-guardoff", seed=seed, sigma=sigma, d=d, k=0, net=off[0],
                         nav=R.value(h, sqrt_at(r))[0]))
        if d <= 60:
            fo = best_attack(h, r, r, (d,), (0,), "fence")[R.WAD]
            rows.append(dict(lane="oneshot-ctl-fenceonly", seed=seed, sigma=sigma, d=d, k=0, net=fo[0],
                             nav=R.value(h, sqrt_at(r))[0]))
    # lane 2: rate, both states, the reference lagging by ℓ
    for variant in ("exit", "in"):
        hs, rs, _ = sample_state(seed, sigma, exit_state=(variant == "exit"))
        for ell in lags(seed, sigma, 3):
            ref = rs - ell
            on = best_attack(hs, rs, ref, (20, 30), (0, 1), True)[R.WAD]
            fo = best_attack(hs, rs, ref, (20, 30), (0,), "fence")[R.WAD]
            # remedy study (reported, not the spec): a third leg at the reference pool's spot = the market ± its fee
            # the attacker picks the deep pool's spot offset that helps it most (r1 R1-02): within its fee band
            # (±5 ticks) and within the fee band plus ~12 s of drift (±10 ticks)
            rm = {}
            for band_u, us in ((5, (-5, -3, 0, 3, 5)), (10, (-10, -7, -5, 0, 5, 7, 10))):
                rm[band_u] = max(best_attack(hs, rs, ref, (20, 30), (0, 1), True, extra=sqrt_at(rs + u))[R.WAD][0]
                                 for u in us)
            rows.append(dict(lane=f"rate-{variant}", seed=seed, sigma=sigma, lag=ell, net=on[0], ctl=fo[0],
                             remedy5=rm[5], remedy10=rm[10], nav=R.value(hs, sqrt_at(rs))[0]))
    # lane 3: JIT
    for d in (0, 20, 60):
        for J in (10 ** 20, 10 ** 22, 10 ** 24):
            j = jit_lane(h, r, d, J)
            if j:
                rows.append(dict(lane="jit", seed=seed, sigma=sigma, d=d, J=J, net=j[0], ctl=j[1], nav=j[2]))
    # lane 4: coerced grief (in-range state)
    for st in ("in", "exit"):   # r1 R1-01: edge60 places from exit states; in-range states stand for the hover trigger
        hi_, ri_, _ = sample_state(seed, sigma, exit_state=(st == "exit"))
        for d in (20, 40, 60):
            g = grief_lane(hi_, ri_, d)
            if g:
                rows.append(dict(lane="grief", state=st, seed=seed, sigma=sigma, d=d, net=g[0], extra_charge=g[1],
                                 nav=R.value(hi_, sqrt_at(ri_))[0]))
    return rows


def summarize(rows):
    ok = True
    res = {}
    bp = lambda r, key="net": r[key] * 1e4 / r["nav"]  # noqa: E731
    print("\nM-2d (cell f)")
    one = [r for r in rows if r["lane"] == "oneshot"]
    mx = max(one, key=lambda r: r["net"])
    blocked = all(r["net"] == 0 for r in one if r["d"] > 60)
    print(f"  lane 1 one-shot: {len(one)} rows, max net {mx['net']} wei ({bp(mx):.4f} bp; d={mx['d']} k={mx['k']}); "
          f"d > REF_BAND always blocked: {blocked}")
    ok &= mx["net"] <= 1 and blocked
    for d in D1:
        for sg in SIGMAS:
            c = [bp(r) for r in rows if r["lane"] == "oneshot-ctl-guardoff" and r["d"] == d and r["sigma"] == sg]
            m, lo, _ = mean_ci(c)
            res[f"guardoff-{d}-{sg}"] = (m, lo)
        print(f"    control guard off d={d}: mean {sum(res[f'guardoff-{d}-{sg}'][0] for sg in SIGMAS) / 3:.2f} bp "
              f"(σ 40/60/120: {', '.join(f'{res[f'guardoff-{d}-{sg}'][0]:.1f}' for sg in SIGMAS)}; "
              f"CI lo min {min(res[f'guardoff-{d}-{sg}'][1] for sg in SIGMAS):.2f})")
    for sg in SIGMAS:
        c = [bp(r) for r in rows if r["lane"] == "oneshot-ctl-fenceonly" and r["sigma"] == sg]
        m, lo, _ = mean_ci(c)
        print(f"    control fence-only (d ≤ 60, pooled) σ {sg}: mean {m:.4f} bp, CI lo {lo:.4f}")
        ok &= lo > 0
    ok &= min(res[f"guardoff-1000-{sg}"][1] for sg in SIGMAS) > 0
    print("  lane 2 rate (lag leak), NAV fraction per year:")
    for sg in SIGMAS:
        rate = rate_c = 0.0
        parts, gas_parts, ci_parts = {}, {}, {}
        for v in ("exit", "in"):
            g = [r for r in rows if r["lane"] == f"rate-{v}" and r["sigma"] == sg]
            n_opp = n_opps(v, sg)
            e = sum(max(0.0, r["net"]) / r["nav"] for r in g) / len(g)
            m_, lo_, hi_ = mean_ci([n_opp * max(0.0, r["net"]) / r["nav"] for r in g])
            ci_parts[v] = (lo_, hi_)
            eg = sum(max(0.0, r["net"] - CYCLE_GAS_WEI) / r["nav"] for r in g) / len(g)
            ec = sum(max(0.0, r["ctl"]) / r["nav"] for r in g) / len(g)
            gas_parts[v] = n_opp * eg
            parts[v] = (len(g), sum(1 for r in g if r["net"] > 0), n_opp * e, n_opp * ec,
                        max(r["net"] * 1e4 / r["nav"] for r in g))
            rate += n_opp * e
            rate_c += n_opp * ec
        b = BOUND[sg]
        rate_gas = sum(gas_parts.values())
        for key, lab in (("remedy5", "± 5 ticks (fee band)"), ("remedy10", "± 10 ticks (fee band + 12 s drift)")):
            rem = 0.0
            for v in ("exit", "in"):
                g = [r for r in rows if r["lane"] == f"rate-{v}" and r["sigma"] == sg]
                rem += n_opps(v, sg) * sum(max(0.0, r[key]) / r["nav"] for r in g) / len(g)
            npos = sum(1 for r in rows if r["lane"].startswith("rate") and r["sigma"] == sg and r[key] > 0)
            print(f"    σ {sg}: remedy study, third leg at the reference pool's spot, attacker-chosen offset {lab}: "
                  f"{rem * 100:.5f}%/yr; positive cycles {npos}")
            res[f"rate_{key}-{sg}"] = rem
        print(f"    σ {sg}: with gas (a cycle = one placement + two swaps, {CYCLE_GAS_WEI / 1e18:.5f} ETH at "
              f"{GAS_GWEI} gwei, NAV ≈ 1,000 WETH): {rate_gas * 100:.5f}%/yr (out {gas_parts['exit'] * 100:.5f}, "
              f"in {gas_parts['in'] * 100:.5f})")
        res[f"rate_gas-{sg}"] = rate_gas
        verdict = "reported" if b is None else ("PASS" if rate <= b else "FAIL")
        print(f"    σ {sg}: 90% CI of the sum ≈ [{(ci_parts['exit'][0] + ci_parts['in'][0]) * 100:.4f}, "
              f"{(ci_parts['exit'][1] + ci_parts['in'][1]) * 100:.4f}]%/yr (out [{ci_parts['exit'][0] * 100:.4f}, "
              f"{ci_parts['exit'][1] * 100:.4f}], in [{ci_parts['in'][0] * 100:.4f}, {ci_parts['in'][1] * 100:.4f}])")
        print(f"    σ {sg}: lag leak {rate * 100:.5f}%/yr (out {parts['exit'][2] * 100:.5f}, in {parts['in'][2] * 100:.5f}; "
              f"positive cycles out {parts['exit'][1]}/{parts['exit'][0]}, in {parts['in'][1]}/{parts['in'][0]}; "
              f"max cycle net out {parts['exit'][4]:.4f} bp, in {parts['in'][4]:.4f} bp) bound {b} -> {verdict}; "
              f"control (reference leg off) {rate_c * 100:.4f}%/yr")
        if b is not None:
            ok &= rate <= b
        ok &= rate_c > 0
        res[f"rate-{sg}"] = (rate, rate_c)
    jit = [r for r in rows if r["lane"] == "jit"]
    mj = max(jit, key=lambda r: r["net"])
    print(f"  lane 3 JIT: {len(jit)} rows, max net {mj['net']} wei ({bp(mj):.4f} bp; d={mj['d']} J={mj['J']:.0e})")
    ok &= mj["net"] <= 1
    for sg in SIGMAS:
        best = {}
        for r in jit:
            if r["sigma"] == sg:
                best[r["seed"]] = max(best.get(r["seed"], 0.0), max(0.0, bp(r, "ctl")))
        m, lo, _ = mean_ci(list(best.values()))
        print(f"    control φ = 10% σ {sg}: mean {m:.4f} bp, CI lo {lo:.4f}, profitable {sum(1 for x in best.values() if x > 0)}")
        ok &= lo > 0
    gr = [r for r in rows if r["lane"] == "grief"]
    if gr:
        for st in ("in", "exit"):
            g2 = [r for r in gr if r["state"] == st]
            if g2:
                m2 = max(g2, key=lambda r: r["net"])
                print(f"    grief, {st} states: {len(g2)} rows, {sum(1 for r in g2 if r['net'] > 0)} > 0, max "
                      f"{bp(m2):.4f} bp (σ {m2['sigma']}, d={m2['d']}), curator extra charge max "
                      f"{max(bp(r, 'extra_charge') for r in g2):.4f} bp")
        ok &= max(r["net"] for r in gr) <= 1   # item 1b's rule (net ≤ 0) applies to the grief lane (r1 R1-01)
        mg = max(gr, key=lambda r: r["net"])
        print(f"  lane 4 coerced grief (gated: net ≤ 0): {len(gr)} rows; griefer max net {mg['net'] / ETH:.5f} ETH "
              f"({bp(mg):.4f} bp; d={mg['d']}); griefer positive rows {sum(1 for r in gr if r['net'] > 0)}; "
              f"curator extra charge p50 / max {pctl([bp(r, 'extra_charge') for r in gr], 0.5):.4f} / "
              f"{max(bp(r, 'extra_charge') for r in gr):.4f} bp")
    print("M-2d verdict:", "PASS" if ok else "FAIL")
    return ok, res


def main():
    os.makedirs(OUT, exist_ok=True)
    seeds = list(TEST_SEEDS)[: int(sys.argv[1])] if len(sys.argv) > 1 else list(TEST_SEEDS)
    jobs = [(s, sg) for sg in SIGMAS for s in seeds]
    t0 = time.time()
    rows = []
    with mp.Pool(9) as pool:
        for i, r in enumerate(pool.imap_unordered(run_path, jobs, chunksize=2)):
            rows.extend(r)
            if (i + 1) % 60 == 0:
                print(f"  {i + 1}/{len(jobs)} paths, {time.time() - t0:.0f}s", flush=True)
    with open(os.path.join(OUT, "m2d.json"), "w") as fh:
        json.dump(rows, fh)
    ok, _ = summarize(rows)
    return ok


if __name__ == "__main__":
    sys.exit(0 if main() else 1)
