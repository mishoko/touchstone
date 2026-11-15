#!/usr/bin/env python3
"""Cell (c): solvency over both curator action types at the fence.

Part A (adversarial worst case). The coalition chooses the state and the action: inventory mix, old
position, spot pushed anywhere within REF_BAND of the reference, the range (any legal aligned W-wide range
containing spot and the reference, with spot optionally moved one unit inside an edge), and for swaps JIT
liquidity at the slippage limit. Measured: charge / NAV_after(p*), the ratio the gate compares with the bond
fraction (burn = ceilDiv(charge·S, NAV') <= bond = BOND_RATIO·S at the NAV cap).

Part B (distribution). Per σ bucket, 200 test paths: the edge60 action from a path state with spot pushed
adversarially to the fence. Reports p50 / p99 / max (Q = the p99 single-action charge, an input of m_PC).

Rule: BOND_RATIO >= 2 × max over action types of the worst ratio; assertion: the worst action of each type
passes the gate at the proposed BOND_RATIO with no unvested pay; positive control: at half the worst ratio
the same action fails the gate.
"""
import json
import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from common import R, sqrt_at, T0, SIGMAS, TEST_SEEDS, W, SPACING, floor_sp, sample_state, edge60_range, pctl  # noqa

ETH = 10 ** 18
NAV_ETH = 1000
FEE = 500
SWAP_CAP = R.TEST.MAX_SWAP_ACTION
BANDS = (20, 60, 100)
THETAS = (0, 1, 2, 5, 10, 20, 30, 40, 50, 60, 70, 80, 85, 90, 95, 98, 99, 100)  # token0 value share, %


def inventory(theta, r=T0):
    s = sqrt_at(r)
    v0 = NAV_ETH * ETH * theta // 100
    return R.Holdings(R.mul_div(v0, 1 << 192, s * s), NAV_ETH * ETH - v0)


def with_old(idle, kind, r=T0):
    """An old position sized from the inventory at r: none, centred, token0-edge or token1-edge."""
    if kind == "none":
        return idle.copy()
    s = sqrt_at(r)
    if kind == "centred":
        lo = floor_sp(r) - W // 2
    elif kind == "edge0":
        lo = floor_sp(r)
    else:
        lo = floor_sp(r) + SPACING - W
    L, h, _ = R.placement_size(idle, lo, lo + W, s, s)
    return h


JIT_SIZES = (10 ** 17, 10 ** 18, 2 * 10 ** 18, 5 * 10 ** 18, 10 ** 19, 10 ** 20, 10 ** 21, 10 ** 24)


def jit_fill(zfo, amount, spot_tick, lim_tick, own=None, jit=10 ** 24):
    """Execute an exact-input swap from spot to the limit with coalition JIT liquidity at the limit and,
    optionally, the vault's own position in the path. Returns (used, out, fees0, fees1 to the own position)."""
    pool = R.Pool(sqrt_at(spot_tick), FEE)
    if own and own.L:
        pool.modify("vault", own.lo, own.hi, own.L)
    j = (lim_tick, lim_tick + SPACING) if zfo else (lim_tick - SPACING, lim_tick)
    pool.modify("jit", j[0], j[1], jit)
    used, out = pool.swap(zfo, amount, sqrt_at(lim_tick))
    f0 = f1 = 0
    if own and own.L:
        _, _, f0, f1 = pool.modify("vault", own.lo, own.hi, 0)
    return used, out, f0, f1


def ratio(before, after, s_spot, s_ref):
    c, p, n, burn, ok = R.measure(before, after, s_spot, s_ref, 10 ** 24, 0, 10 ** 24)
    return (c / n if n else 0.0), c, n, p


def swap_cap_amount(h, zfo, s_ref):
    nav = R.value(h, s_ref)[0]
    cap_val = nav * SWAP_CAP // R.WAD
    amt = R.mul_div(cap_val, 1 << 192, s_ref * s_ref) if zfo else cap_val
    return min(amt, h.idle0 if zfo else h.idle1)


def worst_swap(band):
    best = (0.0,)
    for theta in THETAS:
        for kind in ("none", "centred", "edge0", "edge1"):
            h0 = with_old(inventory(theta), kind)
            for off in (-band, band):
                spot_t = T0 + off
                for zfo in (True, False):
                    s_ref = sqrt_at(T0)
                    amt = swap_cap_amount(h0, zfo, s_ref)
                    if amt == 0:
                        continue
                    lim = spot_t - 10 if zfo else spot_t + 10
                    for jit in JIT_SIZES:  # the JIT size that pushes the fill deepest into the band
                      used, out, f0, f1 = jit_fill(zfo, amt, spot_t, lim, own=h0, jit=jit)
                      h1 = h0.copy()
                      if zfo:
                          h1.idle0, h1.idle1 = h1.idle0 - used, h1.idle1 + out
                      else:
                          h1.idle1, h1.idle0 = h1.idle1 - used, h1.idle0 + out
                      h1.idle0 += f0
                      h1.idle1 += f1
                      rt, c, n, p = ratio(h0, h1, sqrt_at(spot_t), s_ref)
                      if rt > best[0]:
                        best = (rt, dict(theta=theta, old=kind, spot_off=off, zfo=zfo, amount=amt, jit=jit, charge=c, navp=n,
                                         leg="ref" if p == s_ref else "spot", before=h0.d(), after=h1.d(),
                                         sqrtSpot=sqrt_at(spot_t), sqrtRef=s_ref))
    return best


def worst_place(band):
    best = (0.0,)
    s_ref = sqrt_at(T0)
    for theta in THETAS:
        for kind in ("none", "centred", "edge0", "edge1"):
            h0 = with_old(inventory(theta), kind)
            for off in sorted({-band, -band + 10, -band // 2, 0, band // 2, band - 10, band}):
                spot_t = T0 + off
                lo_min = floor_sp(max(spot_t, T0)) + SPACING - W
                lo_max = floor_sp(min(spot_t, T0))
                for lo in range(lo_min, lo_max + 1, SPACING):
                    spots = [sqrt_at(spot_t)]
                    if lo == lo_max and abs(lo - T0) <= band:
                        spots.append(sqrt_at(lo) + 1)          # spot moved one unit above sqrtAt(lo)
                    if lo == lo_min and abs(lo + W - 1 - T0) <= band:
                        spots.append(sqrt_at(lo + W) - 1)      # one unit below sqrtAt(hi)
                    for sp in spots:
                        for pre, jit in [(None, 0)] + [(z, j) for z in (True, False) for j in JIT_SIZES]:
                            hb = h0.copy()
                            if hb.L:
                                x, y = R.position_amounts(hb.L, hb.lo, hb.hi, sp)
                                hb = R.Holdings(hb.idle0 + x, hb.idle1 + y)
                            sp_after = sp
                            if pre is not None:
                                amt = swap_cap_amount(hb, pre, s_ref)
                                if amt == 0:
                                    continue
                                tk = R.get_tick_at_sqrt_price(sp)
                                lim = tk - 10 if pre else tk + 10
                                used, out, _, _ = jit_fill(pre, amt, tk, lim, jit=jit)
                                if pre:
                                    hb.idle0, hb.idle1 = hb.idle0 - used, hb.idle1 + out
                                else:
                                    hb.idle1, hb.idle0 = hb.idle1 - used, hb.idle0 + out
                                sp_after = sqrt_at(lim)  # conservative: spot at the limit (max L for the add)
                            L, h1, d = R.placement_size(hb, lo, lo + W, sp_after, s_ref)
                            if d < R.WAD // 2:
                                continue
                            rt, c, n, p = ratio(h0, h1, sp, s_ref)
                            if rt > best[0]:
                                best = (rt, dict(theta=theta, old=kind, spot_off=off, lo_off=lo - T0, pre=pre,
                                                 edge_spot=sp != sqrt_at(spot_t), charge=c, navp=n, deltaAfter=d,
                                                 leg="ref" if p == s_ref else "spot", before=h0.d(), after=h1.d(),
                                                 sqrtSpot=sp, sqrtRef=s_ref))
    return best


def gate(case, bond_ratio):
    """The gate at the NAV cap with no unvested pay: S shares, bond = BOND_RATIO·S."""
    S = 10 ** 24
    b = R.Holdings(**case["before"])
    a = R.Holdings(**case["after"])
    bond = S * bond_ratio // R.WAD
    return R.measure(b, a, case["sqrtSpot"], case["sqrtRef"], S, 0, bond)[4]


def distribution(band=60):
    """Part B: edge60 actions from path states with spot pushed to the fence in the worse direction."""
    out = {}
    for sigma in SIGMAS:
        vals = []
        for seed in TEST_SEEDS:
            worst = 0.0
            for variant in (False, True):
                h, r, _ = sample_state(seed, sigma, exit_state=variant)
                s_ref = sqrt_at(r)
                for off in (-band, band):
                    sp_t = r + off
                    sp = sqrt_at(sp_t)
                    x, y = R.position_amounts(h.L, h.lo, h.hi, sp)
                    idle = R.Holdings(h.idle0 + x, h.idle1 + y)
                    lo, hi = edge60_range(idle, r, sp_t)
                    L, h1, d = R.placement_size(idle, lo, hi, sp, s_ref)
                    if d < R.WAD // 2:
                        se = sqrt_at(lo) + 1 if lo == floor_sp(min(r, sp_t)) else sqrt_at(hi) - 1
                        if abs(R.get_tick_at_sqrt_price(se) - r) <= band:
                            L, h1, d = R.placement_size(idle, lo, hi, se, s_ref)
                            sp = se
                    if d < R.WAD // 2:
                        continue
                    worst = max(worst, ratio(h, h1, sp, s_ref)[0])
            vals.append(worst)
        out[sigma] = dict(n=len(vals), p50=pctl(vals, 0.5), p99=pctl(vals, 0.99), max=max(vals))
    return out


def main():
    t0 = time.time()
    res = {"bands": {}}
    for band in BANDS:
        ws, wp = worst_swap(band), worst_place(band)
        res["bands"][band] = dict(swap=ws[0], place=wp[0], swap_case=ws[1], place_case=wp[1],
                                  curvature_ref=2.6e-8 * band * band, naive_swap_formula=0.02 * (2 * 0.0005 + 2 * 0.0005))
        print(f"band {band:3d}: worst swap {ws[0]:.3e} ({ws[1]['leg']}, old {ws[1]['old']}, θ {ws[1]['theta']}%), "
              f"worst setPositions {wp[0]:.3e} ({wp[1]['leg']}, old {wp[1]['old']}, θ {wp[1]['theta']}%, "
              f"pre {wp[1]['pre']}, edge spot {wp[1]['edge_spot']}); 2.6e-8·band² = {2.6e-8 * band * band:.2e} "
              f"[{time.time() - t0:.0f}s]", flush=True)
    res["distribution_band60"] = distribution(60)
    res["distribution_band100"] = distribution(100)
    for b in (60, 100):
        for s, v in res[f"distribution_band{b}"].items():
            print(f"  band {b} σ {s}: edge60 at the fence: p50 {v['p50']:.2e}, p99 {v['p99']:.2e}, max {v['max']:.2e} (n {v['n']})")
    # proposal and gate checks
    res["proposal"] = {}
    for band in BANDS:
        w = max(res["bands"][band]["swap"], res["bands"][band]["place"])
        need = 2 * w
        prop = next(x / 10 ** 4 for x in range(1, 100) if x / 10 ** 4 >= need)  # 1e-4 grid
        br = int(prop * 1e18)
        checks = {}
        for typ in ("swap", "place"):
            case = res["bands"][band][f"{typ}_case"]
            checks[typ] = dict(passes_at_proposal=gate(case, br),
                               control_fails_at_half_worst=not gate(case, int(res["bands"][band][typ] / 2 * 1e18)),
                               passes_at_default_5e_4=gate(case, 5 * 10 ** 14))
        res["proposal"][band] = dict(worst=w, two_x=need, BOND_RATIO=prop, checks=checks)
        print(f"band {band}: worst {w:.3e}, 2x = {need:.3e} -> BOND_RATIO {prop:.2e}; checks {checks}")
    out = os.path.join(os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))), "output", "g0")
    os.makedirs(out, exist_ok=True)
    with open(os.path.join(out, "solvency.json"), "w") as fh:
        json.dump(res, fh, indent=1, default=str)
    return res


if __name__ == "__main__":
    main()
