#!/usr/bin/env python3
"""M-5b: a re-ratio over a fee-rich segment. The charge must cover the realised loss.

The vault's position first accrues uncollected fees F: a trader round-trips volume through the vault's range
(n round trips of ±x ticks around the market price r; F/NAV from ≈ 0.05 to ≈ 30 bp). Then the curator changes the
inventory ratio by `frac` of NAV with either action:
- `rebalanceSwap` (the position stays; collect first, swap from idle, collect again after the swap), or
- `setPositions` with a pre-swap (collect first, remove the old position, swap, place a legal W-wide range at spot
  containing the reference, every legal range on an 80-tick grid plus both edges).
Spot may be displaced s ∈ {0, ±20, ±60} ticks from r within the fence (reference = r).

Realised loss = NAV_before(r) − NAV_after(r), where both sides include every fee the vault owns (accrued fees are
vault property whether or not they are collected).
Assertion: on every row, charge ≥ realised loss − 1 wei.
Control: collect-first disabled. NAV_before is then computed from holdings without the uncollected fees, which the
action's own modify pays out into NAV_after, so the fees offset the action's cost. The per-path shortfall
max(0, realised loss − control charge) must have mean > 0 and one-sided 95% CI lower bound > 0 in every σ bucket.
"""
import json
import multiprocessing as mp
import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from common import R, sqrt_at, SIGMAS, TEST_SEEDS, W, SPACING, floor_sp, sample_state, mean_ci  # noqa: E402
from adv3 import with_idle  # noqa: E402

FEE, BAND, SLIP = 500, 60, 10
WASH = ((0, 0), (5, 2), (20, 10), (60, 40))       # (x ticks, round trips)
OFFS = (0, 20, -20, 60, -60)
FRACS = (5 * 10 ** 15, 2 * 10 ** 16)
OUT = os.path.join(os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))), "output", "g0")


def fee_rich_pool(h, r, x, n):
    """The vault's position in a pool after n round trips of ±x ticks (fees uncollected), price back at r."""
    sr = sqrt_at(r)
    pool = R.Pool(sr, FEE)
    if h.L:
        pool.modify("vault", h.lo, h.hi, h.L)
    for _ in range(n):
        pool.push_to(sqrt_at(r + x))
        pool.push_to(sqrt_at(r - x))
    pool.push_to(sr)
    return pool


def legal_ranges(spot, ref):
    lo_min = floor_sp(max(spot, ref)) + SPACING - W
    lo_max = floor_sp(min(spot, ref))
    return sorted(set(list(range(lo_min, lo_max + 1, 80)) + [lo_min, lo_max]))


def act(h, r, x, n, off, frac, zfo, kind, lo_new=None, collect_first=True):
    """Returns (charge, realised loss, uncollected fees value) in wei at r, or None if not legal."""
    sr = sqrt_at(r)
    pool = fee_rich_pool(h, r, x, n)
    unc0 = unc1 = 0
    if h.L:   # the uncollected fees (read without collecting, on a copy of the fee checkpoints)
        p = pool.pos[("vault", h.lo, h.hi)]
        saved = list(p)
        unc0, unc1 = pool.collect("vault", h.lo, h.hi)
        p[1], p[2] = saved[1], saved[2]
    if off:
        pool.push_to(sqrt_at(r + off))        # spot displaced within the fence (a trader's push)
    tick_spot = pool.tick
    if abs(tick_spot - r) > BAND:
        return None
    hb = h.copy()
    if h.L and collect_first:
        _, _, f0, f1 = pool.modify("vault", hb.lo, hb.hi, 0)
        hb.idle0 += f0
        hb.idle1 += f1
    before = hb.copy()
    # the true value before the action includes every fee the vault owns
    true_before = h.copy()
    true_before.idle0 += unc0
    true_before.idle1 += unc1
    s_spot = pool.s
    nav_r = R.value(true_before, sr)[0]
    amt = R.mul_div(nav_r * frac // R.WAD, 1 << 192, sr * sr) if zfo else nav_r * frac // R.WAD
    lim = sqrt_at(tick_spot - SLIP if zfo else tick_spot + SLIP)
    if kind == "swap":
        if amt > (hb.idle0 if zfo else hb.idle1):
            return None
        used, out = pool.swap(zfo, amt, lim)
        if used == 0:
            return None
        after = hb.copy()
        if zfo:
            after.idle0, after.idle1 = after.idle0 - used, after.idle1 + out
        else:
            after.idle1, after.idle0 = after.idle1 - used, after.idle0 + out
        if after.L:
            _, _, f0, f1 = pool.modify("vault", after.lo, after.hi, 0)
            after.idle0 += f0
            after.idle1 += f1
    else:
        if placement_illegal(lo_new, tick_spot, r):
            return None
        if hb.L:
            a0, a1, f0, f1 = pool.modify("vault", hb.lo, hb.hi, -hb.L)
            hb = R.Holdings(hb.idle0 - a0 + f0, hb.idle1 - a1 + f1)
        if amt > (hb.idle0 if zfo else hb.idle1):
            return None
        used, out = pool.swap(zfo, amt, lim)
        if zfo:
            hb.idle0, hb.idle1 = hb.idle0 - used, hb.idle1 + out
        else:
            hb.idle1, hb.idle0 = hb.idle1 - used, hb.idle0 + out
        L, after, dl = R.placement_size(hb, lo_new, lo_new + W, pool.s, sr)
        if dl < R.WAD // 2 or L == 0:
            return None
        pool.modify("vault", lo_new, lo_new + W, L)
    charge = R.measure(before, after, s_spot, sr, 10 ** 24, 0, 10 ** 24)[0]
    realised = nav_r - R.value(after, sr)[0]
    return charge, realised, R.q(unc0, sr) + unc1


def placement_illegal(lo, tick_spot, ref):
    return R.placement_check(lo, lo + W, tick_spot, ref, BAND, W, SPACING) != 0


def run_path(args):
    seed, sigma = args
    rows = []
    for state in ("in", "exit"):
        h0, r, _ = sample_state(seed, sigma, exit_state=(state == "exit"))
        h = with_idle(h0, r)
        nav = R.value(h, sqrt_at(r))[0]
        for (x, n) in WASH:
            for off in OFFS:
                for frac in FRACS:
                    for zfo in (True, False):
                        cand = [("swap", None)] + [("place", lo) for lo in legal_ranges(r + off, r)]
                        for kind, lo in cand:
                            a = act(h, r, x, n, off, frac, zfo, kind, lo)
                            if a is None:
                                continue
                            c = act(h, r, x, n, off, frac, zfo, kind, lo, collect_first=False)
                            row = dict(seed=seed, sigma=sigma, state=state, x=x, n=n, off=off, frac=frac, zfo=zfo,
                                       kind=kind, lo=lo, nav=nav, charge=a[0], realised=a[1], fees=a[2],
                                       ctl_charge=c[0] if c else None)
                            rows.append(row)
    return rows


def summarize(rows):
    gap = max(rows, key=lambda r: r["realised"] - r["charge"])
    ok = gap["realised"] - gap["charge"] <= 1
    res = dict(rows=len(rows), max_uncovered_wei=gap["realised"] - gap["charge"],
               worst_row={k: gap[k] for k in ("sigma", "state", "x", "n", "off", "frac", "zfo", "kind")},
               fees_bp_max=max(r["fees"] * 1e4 / r["nav"] for r in rows),
               realised_bp_max=max(r["realised"] * 1e4 / r["nav"] for r in rows),
               overcharge_bp_p50=sorted((r["charge"] - r["realised"]) * 1e4 / r["nav"] for r in rows)[len(rows) // 2],
               control={})
    print(f"M-5b: {len(rows)} rows; max (realised − charge) {res['max_uncovered_wei']} wei at {res['worst_row']}; "
          f"fees up to {res['fees_bp_max']:.2f} bp, realised loss up to {res['realised_bp_max']:.3f} bp")
    for sg in SIGMAS:
        best = {}
        for r in rows:
            if r["sigma"] == sg and r["ctl_charge"] is not None:
                short = max(0.0, (r["realised"] - r["ctl_charge"]) * 1e4 / r["nav"])
                best[r["seed"]] = max(best.get(r["seed"], 0.0), short)
        xs = list(best.values())
        m, lo, _ = mean_ci(xs)
        res["control"][str(sg)] = dict(n=len(xs), mean_bp=m, ci_lo_bp=lo, positive=sum(1 for v in xs if v > 0))
        print(f"  control (no collect-first) σ {sg}: shortfall per path mean {m:.4f} bp, CI lo {lo:.4f}, "
              f"positive {sum(1 for v in xs if v > 0)}/{len(xs)}")
        ok &= lo > 0
    res["ok"] = ok
    print("M-5b verdict:", "PASS" if ok else "FAIL")
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
    with open(os.path.join(OUT, "m5b.json"), "w") as fh:
        json.dump(rows, fh)
    res = summarize(rows)
    with open(os.path.join(OUT, "m5b-summary.json"), "w") as fh:
        json.dump(res, fh, indent=1, default=str)
    return res["ok"]


if __name__ == "__main__":
    sys.exit(0 if main() else 1)
