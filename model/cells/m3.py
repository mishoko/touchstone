#!/usr/bin/env python3
"""M-3: curator wash trading.

Coalition = curator + a Sybil trader. The Sybil round-trips volume through the vault's range (n round trips of
±x ticks around the market price r, back to r), so the vault's position earns fees F_v paid by the coalition. Then
one of three follow-ups:
- a checkpoint: base accrues on NAV₀, which the fees raised; the extra base is bounded generously by
  m_ub × ΔNAV × δ × one year with m_ub = 10%/yr (ten times any rate the method has produced);
- the curator's `rebalanceSwap` of 2% of NAV from idle;
- the curator's honest in-place edge60 placement.
Coalition net = the Sybil's P&L (fees and impact, valued at r) + the curator's holding fraction of the vault's
change + the extra base − (1 − f) × the change in the action's charge (the charge burns the curator's shares).

Assertion: the maximum net over every row ≤ 1 wei, and for `rebalanceSwap` the charge is identical with and
without the wash up to 1 wei of rounding (collect-first sweeps the wash fees into idle before NAV_before).
Control: a fee-linked pay (the curator is paid the fees its vault earns, s = 1, i.e. above the break-even
s* = 1 − f at depth 0); the optimal attacker's profit per path must have mean > 0 and one-sided 95% CI lower
bound > 0 in every σ bucket. Grid: in-range and exit states; (x, n) ∈ {(5, 2), (20, 10), (60, 40)}; third-party
depth k ∈ {0, 1} × the vault's liquidity on a ±3,000-tick range.
"""
import json
import multiprocessing as mp
import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from common import R, sqrt_at, SIGMAS, TEST_SEEDS, W, SPACING, floor_sp, sample_state, mean_ci, edge60_range  # noqa
from adv3 import with_idle  # noqa: E402

FEE = 500
WASH = ((5, 2), (20, 10), (60, 40))
KS = (0, 1)
F_CAP = 5 * 10 ** 14          # the curator's holding fraction at the NAV cap (WAD)
M_UB = 10 ** 17               # 10%/yr, WAD
S_CTL = R.WAD                 # control: fee-linked pay, s = 1
OUT = os.path.join(os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))), "output", "g0")


def world(h, r, x, n, k):
    """The pool with the vault's position (and third-party depth k) after the wash; returns (pool, sybil value at r)."""
    sr = sqrt_at(r)
    pool = R.Pool(sr, FEE)
    if h.L:
        pool.modify("vault", h.lo, h.hi, h.L)
    third = int(k * (h.L if h.L else 10 ** 17))
    if third:
        pool.modify("lp", floor_sp(r) - 3000, floor_sp(r) + 3000, third)
    s0 = s1 = 0
    for _ in range(n):
        for tgt in (r + x, r - x):
            ain, aout, z = pool.push_to(sqrt_at(tgt))
            s0, s1 = (s0 - ain, s1 + aout) if z else (s0 + aout, s1 - ain)
    ain, aout, z = pool.push_to(sr)
    s0, s1 = (s0 - ain, s1 + aout) if z else (s0 + aout, s1 - ain)
    syb = (R.q(s0, sr) if s0 >= 0 else -R.mul_div_up(-s0, sr * sr, 1 << 192)) + s1
    return pool, syb


def follow_up(pool, h, r, kind):
    """Collect first, then the follow-up. Returns (vault NAV at r after it, charge)."""
    sr = sqrt_at(r)
    hb = h.copy()
    if hb.L:
        _, _, f0, f1 = pool.modify("vault", hb.lo, hb.hi, 0)
        hb.idle0 += f0
        hb.idle1 += f1
    if kind == "checkpoint":
        return R.value(hb, sr)[0], 0, R.delta_of(hb, sr)
    before = hb.copy()
    if kind == "swap":
        zfo = R.q(h.idle0, sr) >= h.idle1        # the action is fixed by the pre-wash state (wash-independent)
        amt = R.value(h, sr)[0] * 2 // 100
        amt = R.mul_div(amt, 1 << 192, sr * sr) if zfo else amt
        used, out = pool.swap(zfo, amt, sqrt_at(pool.tick - 10 if zfo else pool.tick + 10))
        after = hb.copy()
        if zfo:
            after.idle0, after.idle1 = after.idle0 - used, after.idle1 + out
        else:
            after.idle1, after.idle0 = after.idle1 - used, after.idle0 + out
        if after.L:
            _, _, f0, f1 = pool.modify("vault", after.lo, after.hi, 0)
            after.idle0 += f0
            after.idle1 += f1
    else:   # honest in-place edge60 placement at spot = r = the reference
        x0, y0 = R.position_amounts(hb.L, hb.lo, hb.hi, sr) if hb.L else (0, 0)
        lo, hi = edge60_range(R.Holdings(hb.idle0 + x0, hb.idle1 + y0), r, r)
        if R.placement_check(lo, hi, pool.tick, r, 60, W, SPACING):
            return None
        if hb.L:
            a0, a1, _, _ = pool.modify("vault", hb.lo, hb.hi, -hb.L)
            hb = R.Holdings(hb.idle0 - a0, hb.idle1 - a1)
        L, after, dl = R.placement_size(hb, lo, hi, pool.s, sr)
        if dl < R.WAD // 2 or L == 0:
            return None
        pool.modify("vault", lo, hi, L)
    charge = R.measure(before, after, pool.s if kind == "place" else sr, sr, 10 ** 24, 0, 10 ** 24)[0]
    return R.value(after, sr)[0], charge, R.delta_of(after, sr)


def run_path(args):
    seed, sigma = args
    rows = []
    for state in ("in", "exit"):
        h0, r, _ = sample_state(seed, sigma, exit_state=(state == "exit"))
        h = with_idle(h0, r)
        nav = R.value(h, sqrt_at(r))[0]
        for kind in ("checkpoint", "swap", "place"):
            for k in KS:
                base_pool, _ = world(h, r, 0, 0, k)
                b = follow_up(base_pool, h, r, kind)
                if b is None:
                    continue
                for (x, n) in WASH:
                    pool, syb = world(h, r, x, n, k)
                    w = follow_up(pool, h, r, kind)
                    if w is None:
                        continue
                    d_nav = w[0] - b[0]                          # the vault's change at r (fees, net of the action)
                    d_charge = w[1] - b[1]
                    fees_v = d_nav + d_charge                    # the wash fees credited to the vault (estimate)
                    extra_base = max(0, M_UB * max(0, fees_v) // R.WAD * w[2] // R.WAD)
                    cur = F_CAP * d_nav // R.WAD - (R.WAD - F_CAP) * d_charge // R.WAD
                    net = syb + cur + extra_base
                    ctl = syb + F_CAP * d_nav // R.WAD + S_CTL * max(0, fees_v) // R.WAD
                    rows.append(dict(seed=seed, sigma=sigma, state=state, kind=kind, k=k, x=x, n=n, nav=nav,
                                     fees_bp=fees_v * 1e4 / nav, sybil=syb, d_charge=d_charge, net=net, ctl=ctl))
    return rows


def summarize(rows):
    mx = max(rows, key=lambda r: r["net"])
    sw = [r for r in rows if r["kind"] == "swap"]
    ok = mx["net"] <= 1 and all(abs(r["d_charge"]) <= 1 for r in sw)
    pl = [abs(r["d_charge"]) * 1e4 / r["nav"] for r in rows if r["kind"] == "place"]
    res = dict(rows=len(rows), max_net_wei=mx["net"], max_net_bp=mx["net"] * 1e4 / mx["nav"],
               max_row={k: mx[k] for k in ("sigma", "state", "kind", "k", "x", "n")},
               swap_charge_unchanged=all(abs(r["d_charge"]) <= 1 for r in sw),
               place_charge_change_max_bp=max(pl) if pl else 0, fees_bp_max=max(r["fees_bp"] for r in rows),
               control={})
    print(f"M-3: {len(rows)} rows; max net {mx['net']} wei ({res['max_net_bp']:.4f} bp) at {res['max_row']}; "
          f"wash fees up to {res['fees_bp_max']:.2f} bp; rebalanceSwap charge unchanged (≤ 1 wei): {res['swap_charge_unchanged']}; "
          f"placement charge change max {res['place_charge_change_max_bp']:.6f} bp")
    for sg in SIGMAS:
        best = {}
        for r in rows:
            if r["sigma"] == sg:
                best[r["seed"]] = max(best.get(r["seed"], 0.0), max(0.0, r["ctl"] * 1e4 / r["nav"]))
        xs = list(best.values())
        m, lo, _ = mean_ci(xs)
        res["control"][str(sg)] = dict(n=len(xs), mean_bp=m, ci_lo_bp=lo, profitable=sum(1 for v in xs if v > 0))
        print(f"  control (fee-linked pay, s = 1) σ {sg}: mean {m:.5f} bp, CI lo {lo:.5f}, profitable {res['control'][str(sg)]['profitable']}/{len(xs)}")
        ok &= lo > 0
    res["ok"] = ok
    print("M-3 verdict:", "PASS" if ok else "FAIL")
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
    with open(os.path.join(OUT, "m3.json"), "w") as fh:
        json.dump(rows, fh)
    res = summarize(rows)
    with open(os.path.join(OUT, "m3-summary.json"), "w") as fh:
        json.dump(res, fh, indent=1, default=str)
    return res["ok"]


if __name__ == "__main__":
    sys.exit(0 if main() else 1)
