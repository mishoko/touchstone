#!/usr/bin/env python3
"""ADV-3: just-in-time self-dealing on the curator's own swap.

Coalition = curator + a Sybil with ordering control. The Sybil may push spot d ticks against the curator, then adds
just-in-time liquidity J on the ticks the curator's swap will cross (the swap's own slippage band, spot to
spot ∓ (FEE_TICKS + BAND_SWAP) = 10 ticks on the adverse side). The curator swaps `frac` of NAV in a chosen direction:
- `rebalanceSwap` (the position stays in place; the swap runs from idle and also trades through the vault's own
  liquidity; fees are collected again after the swap), or
- a `setPositions` pre-swap (the old position is removed to idle first, so the swap trades only through the JIT
  liquidity and any third-party depth).
The Sybil then removes the JIT position and swaps the price back to the market price r. Everything is valued at r;
the curator holds the NAV-cap fraction of the supply and its charge is burned in shares (`n4.coalition_net`).

Grid: in-range and exit states from an edge60-managed path; d ∈ {0, 20, 30, 60}; J ∈ {1e20, 1e22, 1e24};
both swap directions; frac ∈ {0.5%, 2%} (2% = MAX_SWAP_ACTION); third-party depth k ∈ {0, 1} × the vault's liquidity
on a ±3,000-tick range; REF_BAND 60; the reference = the market price.
Assertion: the coalition's maximum net over every row ≤ 1 wei.
Control: the same search with the charge scaled to φ = 10%: the optimal attacker's profit per path (best row, or 0)
has mean > 0 and one-sided 95% CI lower bound > 0 in every σ bucket.
"""
import json
import multiprocessing as mp
import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from common import R, sqrt_at, SIGMAS, TEST_SEEDS, floor_sp, sample_state, mean_ci  # noqa: E402
from n4 import coalition_net, F_CAP  # noqa: E402

DS = (0, 20, 30, 60)
JS = (10 ** 20, 10 ** 22, 10 ** 24)
FRACS = (5 * 10 ** 15, 2 * 10 ** 16)   # WAD fractions of NAV: 0.5%, 2%
KS = (0, 1)
BAND, SLIP, FEE = 60, 10, 500
OUT = os.path.join(os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))), "output", "g0")


def with_idle(h, r, frac_wad=25 * 10 ** 15):
    """Give the vault idle inventory of 2.5% of its NAV in each token, so that a swap of MAX_SWAP_ACTION (2%) of the
    post-top-up NAV is legal in both directions (a rebalanceSwap runs from idle only)."""
    s = sqrt_at(r)
    nav = R.value(h, s)[0]
    g = h.copy()
    g.idle1 += nav * frac_wad // R.WAD
    g.idle0 += R.mul_div(nav * frac_wad // R.WAD, 1 << 192, s * s)
    return g


def swap_attack(h, r, d, J, zfo, frac, keep, k):
    """One row. Returns the action dict for `coalition_net`, or None if the swap is not legal or empty."""
    sr = sqrt_at(r)
    pool = R.Pool(sr, FEE)
    hb = h.copy()
    if hb.L:
        pool.modify("vault", hb.lo, hb.hi, hb.L)
    third = int(k * (hb.L if hb.L else 10 ** 17))
    if third:
        pool.modify("lp", floor_sp(r) - 3000, floor_sp(r) + 3000, third)
    nav0 = R.value(hb, sr)[0]
    syb0 = syb1 = 0
    if d:
        ain, aout, z = pool.push_to(sqrt_at(r + (-d if zfo else d)))   # adverse to the curator's swap
        syb0, syb1 = (-ain, aout) if z else (aout, -ain)
    tick_spot = pool.tick
    if abs(tick_spot - r) > BAND:     # the fence (reference = market)
        return None
    lim = tick_spot - SLIP if zfo else tick_spot + SLIP
    jlo, jhi = (floor_sp(lim), floor_sp(tick_spot) + 10) if zfo else (floor_sp(tick_spot), floor_sp(lim) + 10)
    if J:
        j0, j1, _, _ = pool.modify("jit", jlo, jhi, J)
        syb0, syb1 = syb0 - j0, syb1 - j1
    # the curator's action: collect first
    if hb.L:
        _, _, f0, f1 = pool.modify("vault", hb.lo, hb.hi, 0)
        hb.idle0 += f0
        hb.idle1 += f1
    before = hb.copy()
    s_spot = pool.s
    if not keep and hb.L:             # a pre-swap: the old position is removed to idle first
        a0, a1, _, _ = pool.modify("vault", hb.lo, hb.hi, -hb.L)
        hb = R.Holdings(hb.idle0 - a0, hb.idle1 - a1)
    amt = R.mul_div(nav0 * frac // R.WAD, 1 << 192, sr * sr) if zfo else nav0 * frac // R.WAD
    if amt == 0 or amt > (hb.idle0 if zfo else hb.idle1):
        return None
    used, out = pool.swap(zfo, amt, sqrt_at(lim))
    if used == 0:
        return None
    after = hb.copy()
    if zfo:
        after.idle0, after.idle1 = after.idle0 - used, after.idle1 + out
    else:
        after.idle1, after.idle0 = after.idle1 - used, after.idle0 + out
    if after.L:                       # rebalanceSwap collects again after the swap
        _, _, f0, f1 = pool.modify("vault", after.lo, after.hi, 0)
        after.idle0 += f0
        after.idle1 += f1
    charge, _, nav_after, _, _ = R.measure(before, after, s_spot, sr, 10 ** 24, 0, 10 ** 24)
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
    return dict(sybil=sybil, nav0=nav0, nav1=nav1, charge=charge, navp=nav_after)


def run_path(args):
    seed, sigma = args
    rows = []
    for state in ("in", "exit"):
        h0, r, _ = sample_state(seed, sigma, exit_state=(state == "exit"))
        h = with_idle(h0, r)
        for keep in (True, False):
            for d in DS:
                for J in JS:
                    for zfo in (True, False):
                        for frac in FRACS:
                            for k in KS:
                                a = swap_attack(h, r, d, J, zfo, frac, keep, k)
                                if a:
                                    rows.append(dict(seed=seed, sigma=sigma, state=state, keep=keep, d=d, J=J,
                                                     zfo=zfo, frac=frac, k=k, nav=a["nav0"], charge=a["charge"],
                                                     net=coalition_net(a, R.WAD, F_CAP),
                                                     ctl=coalition_net(a, R.WAD // 10, F_CAP),
                                                     off=coalition_net(a, 0, F_CAP)))
    return rows


def summarize(rows):
    mx = max(rows, key=lambda r: r["net"])
    ok = mx["net"] <= 1
    res = dict(rows=len(rows), max_net_wei=mx["net"], max_net_bp=mx["net"] * 1e4 / mx["nav"],
               max_row={k: mx[k] for k in ("sigma", "state", "keep", "d", "J", "zfo", "frac", "k")},
               max_charge_bp=max(r["charge"] * 1e4 / r["nav"] for r in rows), control={})
    print(f"ADV-3: {len(rows)} rows; max net {mx['net']} wei ({res['max_net_bp']:.5f} bp) at {res['max_row']}")
    for keep in (True, False):
        g = [r for r in rows if r["keep"] == keep]
        m = max(g, key=lambda r: r["net"])
        print(f"  {'rebalanceSwap' if keep else 'pre-swap'}: {len(g)} rows, max net {m['net']} wei")
        res[f"max_net_{'swap' if keep else 'preswap'}"] = m["net"]
    for sg in SIGMAS:
        for key, name in (("ctl", "phi10"), ("off", "charge off")):
            best = {}
            for r in rows:
                if r["sigma"] == sg:
                    best[r["seed"]] = max(best.get(r["seed"], 0.0), max(0.0, r[key] * 1e4 / r["nav"]))
            xs = list(best.values())
            m, lo, _ = mean_ci(xs)
            res["control"][f"{name}@{sg}"] = dict(n=len(xs), mean_bp=m, ci_lo_bp=lo, profitable=sum(1 for x in xs if x > 0))
            print(f"  control {name:10s} σ {sg}: n={len(xs)} mean {m:.4f} bp, CI lo {lo:.4f}, profitable {sum(1 for x in xs if x > 0)}")
            if key == "ctl":
                ok &= lo > 0
    for keep in (True, False):
        for frac in FRACS:
            n_ = sum(1 for r in rows if r["keep"] == keep and r["frac"] == frac)
            print(f"  rows present: {'rebalanceSwap' if keep else 'pre-swap'} at {frac / 1e16:.1f}% of NAV: {n_}")
            res[f"rows_{'swap' if keep else 'preswap'}_{frac}"] = n_
    res["ok"] = ok
    print("ADV-3 verdict:", "PASS" if ok else "FAIL")
    return res


def main():
    seeds = list(TEST_SEEDS)[: int(sys.argv[1])] if len(sys.argv) > 1 else list(TEST_SEEDS)
    jobs = [(s, sg) for sg in SIGMAS for s in seeds]
    t0 = time.time()
    rows = []
    with mp.Pool(9) as pool:
        for i, r in enumerate(pool.imap_unordered(run_path, jobs, chunksize=2)):
            rows.extend(r)
            if (i + 1) % 100 == 0:
                print(f"  {i + 1}/{len(jobs)} paths, {time.time() - t0:.0f}s", flush=True)
    os.makedirs(OUT, exist_ok=True)
    with open(os.path.join(OUT, "adv3.json"), "w") as fh:
        json.dump(rows, fh)
    res = summarize(rows)
    with open(os.path.join(OUT, "adv3-summary.json"), "w") as fh:
        json.dump(res, fh, indent=1, default=str)
    return res["ok"]


if __name__ == "__main__":
    sys.exit(0 if main() else 1)
