#!/usr/bin/env python3
"""Cell (d): N4 self-sandwich around a no-swap setPositions.

Coalition = curator + a Sybil trader. The Sybil pushes the pool price d ticks from the market price r,
the curator re-places the vault at the pushed spot (no pre-swap; any legal W-wide aligned range containing
spot and the reference, chosen to maximise the coalition's gain over a 40-tick grid of
legal ranges, both edges, and a ±30-tick refinement around the best), and the Sybil swaps the price back to r.
Everything is valued at r. The curator's wealth change counts its holding fraction f of the vault and the
burn of its deferred pay and bond for the charge (scaled by φ: φ = 1 is Touchstone, φ < 1 the controls).

Gating variant: the reference equals the market price r (it is not moved). A lagging-reference
variant is reported separately; it is the lag lane of cell (f) M-2d (s2), which has its own pre-registered
bound.

Pushes: d ∈ {20 (= 4·FEE_TICKS, "4× fee"), 30, 100} ticks; REF_BAND 60 (100 for d = 100); pool fee 5 bp;
third-party depth k ∈ {0, 1} × the vault's liquidity on a wide range.
"""
import json
import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from common import (R, sqrt_at, T0, SIGMAS, TEST_SEEDS, W, SPACING, floor_sp, sample_state, mean_ci,  # noqa
                    pctl)

S_TOT = 10 ** 24
F_CAP = 5 * 10 ** 14  # curator holding fraction at the NAV cap ≈ BOND_RATIO (WAD); f = 0 also reported
PHIS = (R.WAD, R.WAD // 10, 0)
DS = (20, 30, 100)
KS = (0, 1)
FEE = 500


def run_attack(h, r, ref, d_signed, lo, k, band):
    """Execute push -> setPositions -> revert. Returns None if the placement is illegal, else a dict with
    the Sybil's P&L, the vault NAVs, the charge and navAfterPStar (all token1 wei at sqrt_at(r))."""
    sr = sqrt_at(r)
    pool = R.Pool(sr, FEE)
    if h.L:
        pool.modify("vault", h.lo, h.hi, h.L)
    third = k * (h.L if h.L else 10 ** 17)
    if third:
        pool.modify("lp", floor_sp(r) - 3000, floor_sp(r) + 3000, third)
    nav0 = R.value(h, sr)[0]
    # 1. push
    ain, aout, zfo = pool.push_to(sqrt_at(r + d_signed))
    syb0, syb1 = (-ain, aout) if zfo else (aout, -ain)
    tick_spot = pool.tick
    if R.placement_check(lo, lo + W, tick_spot, ref, band, W, SPACING):
        return None
    # 2. the curator's action: collect first, remove, size at spot, add
    hb = h.copy()
    if hb.L:
        _, _, f0, f1 = pool.modify("vault", hb.lo, hb.hi, 0)
        hb.idle0 += f0
        hb.idle1 += f1
    before = hb.copy()
    s_spot, s_ref = pool.s, sqrt_at(ref)
    if hb.L:
        a0, a1, _, _ = pool.modify("vault", hb.lo, hb.hi, -hb.L)
        hb = R.Holdings(hb.idle0 - a0, hb.idle1 - a1)
    L, after, dl = R.placement_size(hb, lo, lo + W, pool.s, s_ref)
    if dl < R.WAD // 2 or L == 0:
        return None
    pool.modify("vault", lo, lo + W, L)
    charge, p_star, nav_after, burn, ok = R.measure(before, after, s_spot, s_ref, S_TOT, 0, S_TOT)
    # 3. revert to r
    ain, aout, zfo = pool.push_to(sr)
    if zfo:
        syb0, syb1 = syb0 - ain, syb1 + aout
    else:
        syb0, syb1 = syb0 + aout, syb1 - ain
    _, _, f0, f1 = pool.modify("vault", lo, lo + W, 0)
    hf = after.copy()
    hf.idle0 += f0
    hf.idle1 += f1
    nav1 = R.value(hf, sr)[0]
    sybil = R.q(syb0, sr) + syb1 if syb0 >= 0 else syb1 - R.mul_div_up(-syb0, sr * sr, 1 << 192)
    return dict(sybil=sybil, nav0=nav0, nav1=nav1, charge=charge, navp=nav_after)


def coalition_net(a, phi, f):
    """Coalition = Sybil P&L + the curator's wealth change (holding f of S, burning φ·charge in shares)."""
    burn = 0
    if phi and a["charge"]:
        c = a["charge"] * phi // R.WAD
        burn = -((-c * S_TOT) // a["navp"]) if c else 0
    B = f * S_TOT // R.WAD
    cur = (B - burn) * a["nav1"] // (S_TOT - burn) - B * a["nav0"] // S_TOT if B >= burn else \
        -burn * a["nav1"] // (S_TOT - burn) - B * a["nav0"] // S_TOT
    return a["sybil"] + cur


def candidates(r, spot, ref):
    lo_min = floor_sp(max(spot, ref)) + SPACING - W
    lo_max = floor_sp(min(spot, ref))
    xs = list(range(lo_min, lo_max + 1, 40)) + [lo_min, lo_max, lo_min + SPACING, lo_max - SPACING]
    return sorted(set(x for x in xs if lo_min <= x <= lo_max))


def best_for_path(h, r, d, k, lag=0):
    """The coalition's best (over direction and range) net for each φ and f; returns per-φ dicts."""
    band = 60 if d <= 60 else 100
    ref = r - lag
    best = {}
    for sgn in (1, -1):
        spot_t = r + sgn * d
        if abs(spot_t - ref) > band:
            continue
        cache = {}
        cand = candidates(r, spot_t, ref)
        for lo in cand:
            cache[lo] = run_attack(h, r, ref, sgn * d, lo, k, band)
        # local refinement around the best candidate for the undefended attack (φ = 0)
        legal = [lo for lo in cand if cache[lo]]
        if legal:
            top = max(legal, key=lambda lo: coalition_net(cache[lo], 0, F_CAP))
            for lo in range(top - 30, top + 40, SPACING):
                if lo not in cache:
                    cache[lo] = run_attack(h, r, ref, sgn * d, lo, k, band)
        for lo, a in cache.items():
            if not a:
                continue
            for phi in PHIS:
                for f in (F_CAP, 0):
                    n = coalition_net(a, phi, f)
                    key = (phi, f)
                    if key not in best or n > best[key][0]:
                        best[key] = (n, a["nav0"], a["charge"], lo, sgn)
    return best


def main(quick=False):
    seeds = list(TEST_SEEDS)[: (20 if quick else len(TEST_SEEDS))]
    t0 = time.time()
    rows = []
    for sigma in SIGMAS:
        for seed in seeds:
          for variant in ("in", "exit"):
            h, r, _ = sample_state(seed, sigma, exit_state=(variant == "exit"))
            for d in DS:
                for k in KS:
                    b = best_for_path(h, r, d, k)
                    if not b:
                        continue
                    rows.append(dict(sigma=sigma, seed=seed, variant=variant, d=d, k=k, lag=0,
                                     **{f"net_{p}_{f}": b[(p, f)][0] for p in PHIS for f in (F_CAP, 0) if (p, f) in b},
                                     nav=b[(R.WAD, F_CAP)][1], charge=b[(R.WAD, F_CAP)][2]))
            # lag variant (reported): reference lags the market by up to band - d, adverse sign chosen by search
            for d in (20, 30) if variant == "in" else ():
                for lag in (30, -30):
                    b = best_for_path(h, r, d, 0, lag=lag)
                    if b:
                        rows.append(dict(sigma=sigma, seed=seed, d=d, k=0, lag=lag,
                                         **{f"net_{p}_{f}": b[(p, f)][0] for p in PHIS for f in (F_CAP, 0)
                                            if (p, f) in b}, nav=b[(R.WAD, F_CAP)][1], charge=b[(R.WAD, F_CAP)][2]))
        print(f"  sigma {sigma}: {len(rows)} rows, {time.time() - t0:.0f}s", flush=True)
    out = os.path.join(os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))), "output", "g0")
    os.makedirs(out, exist_ok=True)
    with open(os.path.join(out, "n4.json"), "w") as fh:
        json.dump(rows, fh)
    return summarize(rows)


def summarize(rows):
    """Assertion: the maximum net over every path, state, push, depth, direction and range at φ = 100% is
    <= 1 wei (one curator action of rounding). Control (fixed after a 20-seed smoke run, see results.md):
    the optimal attacker's profit per path, max(0, best over the in-range and exit states) at k = 0, with the
    charge scaled to φ = 10%; it must have mean > 0 and one-sided 95% CI lower bound > 0 in every σ bucket."""
    res = {}
    full = f"net_{R.WAD}_{F_CAP}"
    ok_all = True
    print("\nN4 (cell d), gating rows (reference = market price); nets in bp of NAV")
    for d in DS:
        g = [r for r in rows if r["d"] == d and r["lag"] == 0]
        if not g:
            continue
        mx = max(max(r[full], r[f"net_{R.WAD}_0"]) for r in g)
        assert_ok = mx <= 1
        per_bucket = {}
        ctl_ok = True
        for sigma in SIGMAS:
            for phi, name in ((R.WAD // 10, "phi10"), (0, "off")):
                best = {seed: 0.0 for seed in TEST_SEEDS}  # a path with no legal attack: the attacker abstains
                for r in g:
                    if r["sigma"] != sigma or r["k"] != 0:
                        continue
                    v = max(0.0, r[f"net_{phi}_{F_CAP}"] * 1e4 / r["nav"])
                    best[r["seed"]] = max(best.get(r["seed"], 0.0), v)
                xs = list(best.values())
                m, lo, _ = mean_ci(xs)
                per_bucket[f"{name}@{sigma}"] = dict(n=len(xs), mean_bp=m, ci_lo_bp=lo,
                                                     profitable=sum(1 for x in xs if x > 0))
                if name == "phi10":
                    ctl_ok &= lo > 0
        ok_all &= assert_ok and ctl_ok
        nets = [r[full] * 1e4 / r["nav"] for r in g]
        res[d] = dict(rows=len(g), max_net_wei=mx, max_net_bp=max(nets), p50_net_bp=pctl(nets, 0.5),
                      max_charge_bp=max(r["charge"] * 1e4 / r["nav"] for r in g), control=per_bucket,
                      verdict=("PASS" if assert_ok else "FAIL") + " / " + ("control > 0" if ctl_ok else "CONTROL FAILS"))
        print(f"  d={d:3d}: rows {len(g)}, max net {mx} wei ({max(nets):.4f} bp) -> {res[d]['verdict']}")
        for kk, v in per_bucket.items():
            print(f"      {kk:10s} n={v['n']} mean {v['mean_bp']:.4f} bp, CI lo {v['ci_lo_bp']:.4f}, "
                  f"profitable paths {v['profitable']}")
    lag = [r for r in rows if r["lag"] != 0]
    if lag:
        nb = [r[full] * 1e4 / r["nav"] for r in lag]
        pos = [x for x in nb if x > 0]
        res["lag"] = dict(rows=len(lag), max_net_bp=max(nb), mean_net_bp=sum(nb) / len(nb), positive=len(pos),
                          max_net_wei=max(r[full] for r in lag))
        print(f"  lag variant (reported; M-2d lag lane): {len(lag)} rows, max net {max(nb):.5f} bp "
              f"({res['lag']['max_net_wei']} wei), mean {sum(nb) / len(nb):.4f} bp, {len(pos)} rows > 0")
    res["ok"] = ok_all
    return res


if __name__ == "__main__":
    r = main(quick="--quick" in sys.argv)
    print(json.dumps(r, indent=1, default=str))
