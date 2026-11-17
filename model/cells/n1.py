#!/usr/bin/env python3
"""Cell (b): N1, pushing the pool price around a checkpoint to move pay.

The execution charge is zero between actions; the base fee's τ_v needs the pool price strictly inside the range
AND the fresh cached reference in range; the mint price is the cached (or live) reference. So a push of the pool
price can move pay only through τ_v, and only while the cached reference is in range.

Setup (mainnet tier, the full model through `sim.Sim`): an edge60-managed vault is run on a 60-day path to a
state: "in" (a random hour with the market in range) or "exit" (the first hour out of range after it, before the
curator re-places; the cached reference may still be in range). The world is forked:
- baseline ("hold"): the coalition has ordering control but does not push: no arbitrage for Δ ∈ {1, 2} hours, the
  checkpoint (a poke) first at T + Δ, then the coalition arbitrages the pool (its gain counts);
- attack: in the next block a Sybil pushes the pool price p ∈ {1, 5, 10, 50} % from the market (either
  direction; third-party depth k ∈ {0, 1} × the vault's liquidity on a ±30 % range), holds it to the checkpoint
  at T + Δ, and swaps back to the pool price the baseline ends at. The Sybil's legs and the baseline's arbitrage
  are valued at the market at T + Δ; both branches move the pool from the same start to the same end price.
The market is held at its price at T over the window in both branches (a static market, as in cells (d) and (g)),
so the net measures the push against the pay it moves rather than a directional bet over Δ. Both branches share
the ordering control, so the net isolates the push (holding arbitrage off is ordinary LVR
extraction that exists in any AMM and does not touch pay).
The Sybil reverts the push to exactly the pool price the baseline's arbitrage leaves at T + Δ, and the coalition is
the arbitrageur in both branches. Coalition net = (the Sybil's P&L − the baseline arbitrage gain), both at the
market price, + the curator's pay change (base accrued, attack − baseline) + the curator's holding fraction
(BOND_RATIO, at the NAV cap) of the vault's NAV change.
Assertion (pre-registered pass bar): max net over every path, state, push, direction, Δ and k ≤ 0 (1 wei per
action of rounding); and the charge is unchanged (exactly 0 in both branches: no curator action).
Parking lane (disclosed): the push of one raw unit inside the exited edge, reported with the bound "τ_v credit
≤ GRID·(1 + ⌈REF_WINDOW/GRID_SEC⌉) per exit" (one anchor plus the anchors the TWAP needs to cross the edge), which
is 2·GRID on the mainnet and DEMO tiers (REF_WINDOW ≤ GRID_SEC); the Foundry cell M-N1 measures it on v4.
Positive control (the defence disabled: τ_v on the pool price alone): the optimal attacker's profit per path
(the best push, parking included, or abstaining) at k = 0 is > 0 with a one-sided 95% CI lower bound > 0.
"""
import copy
import json
import math
import os
import random
import sys
import time
import multiprocessing as mp

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import sim as S  # noqa: E402
import bots as B  # noqa: E402
from common import R, sqrt_at, T0, SIGMAS, TEST_SEEDS, ETH, mean_ci, pctl  # noqa: E402

PUSHES = (0.01, 0.05, 0.10, 0.50)
DELTAS = (1, 2)
KS = (0, 1)
M_YEAR = 0.01
F_CAP = 6e-4          # the curator's holding fraction at the NAV cap (= BOND_RATIO)
OUT = os.path.join(os.path.dirname(os.path.dirname(HERE)), "output", "g0")


def ticks_of(p):
    return int(round(math.log(1 + p) / math.log(1.0001)))


def val_signed(d0, d1, ms):
    """A trade's token deltas (received > 0) valued in token1 at sqrt price ms."""
    return (R.q(d0, ms) if d0 >= 0 else -R.mul_div_up(-d0, ms * ms, 1 << 192)) + d1


def fork(sim):
    return copy.deepcopy(sim, {id(sim.path): sim.path})


def add_third(sim, k):
    if k:
        h = sim.v.h
        pool = sim.v.pool
        c = pool.tick // 10 * 10
        pool.modify("lp", c - 2600, c + 2600, k * max(h.L, 10 ** 15))


def pay(sim):
    return sum(r["base"] for r in sim.v.records.values()) + sim.v.baseOwed


def charges(sim):
    return sum(r["charge"] for r in sim.v.records.values())


def state(seed, sigma, variant):
    rng = random.Random(seed * 31 + int(sigma * 100))
    path = S.market_path(seed, sigma, 61)
    sim = S.Sim(path, M_YEAR)
    pol = B.make("edge60")
    stop = rng.randrange(24 * 7, 24 * 58)
    G = sim.t.GRID
    for hr in range(1, 24 * 60):
        sim.now = sim.B0 + hr * G
        h = sim.v.h
        out = not (h.lo <= sim.mkt() < h.hi)
        if hr >= stop and ((variant == "in" and not out) or (variant == "exit" and out)):
            sim.arb()
            return sim, pol
        pol.pre(sim)
        sim.arb()
        pol.post(sim)
        sim.v.hook.events.clear()
        sim.v.events.clear()
    return None, None


def branch_baseline(sim0, dlt, k):
    """The same ordering control without the push: no arbitrage until T + Δ, the checkpoint first at T + Δ, then the
    coalition arbitrages (its gain counts). The attack reverts to the pool price this branch ends at, so the two
    branches differ only by the push."""
    s = fork(sim0)
    add_third(s, k)
    s.arb_pnl = 0
    s.now = s.now + dlt * s.t.GRID
    s.poke()
    s.arb()
    return s


def branch_attack(sim0, target_tick, dlt, k, exact_sqrt=None, end_sqrt=None):
    s = fork(sim0)
    add_third(s, k)
    G = s.t.GRID
    t_now = s.now
    pool = s.v.pool
    s.now = t_now + 1
    s.touch_swap()
    tgt = exact_sqrt if exact_sqrt is not None else sqrt_at(target_tick)
    ain, aout, zfo = pool.push_to(tgt)
    d1 = (-ain, aout) if zfo else (aout, -ain)
    s.now = t_now + dlt * G
    hk = s.v.hook
    # control: the span the pushed price would credit with τ_v on the pool price alone
    trunc = max(0, min(s.now, hk.refCachedAt + 2 * G) - hk.lastSnap)
    lo, hi = hk.vaultLo, hk.vaultHi
    inside = not hk.emptyRange and sqrt_at(lo) < pool.s < sqrt_at(hi)
    tauv_before = hk.tauV
    s.poke()
    credited = hk.tauV - tauv_before
    ctl_extra = s.v._base_amount(trunc) - s.v._base_amount(credited) if inside else 0
    ain, aout, zfo = pool.push_to(end_sqrt)  # back to where the baseline's arbitrage leaves the pool
    d2 = (-ain, aout) if zfo else (aout, -ain)
    # both legs at the market at T + Δ, like the baseline's arbitrage: the two branches move the pool from the same
    # start price to the same end price, so the token deltas differ only by the push round trip (its fees)
    sybil = val_signed(d1[0] + d2[0], d1[1] + d2[1], s.mkt_sqrt())
    return s, sybil, ctl_extra, credited


def run_path(args):
    seed, sigma = args
    rows = []
    for variant in ("in", "exit"):
        sim0, _ = state(seed, sigma, variant)
        if sim0 is None:
            continue
        # a static market over the attack window (as in cells (d) and (g)): the net measures the push against
        # the pay it moves, not a directional bet on the market over Δ
        i0 = sim0.idx(sim0.ts())
        frozen = list(sim0.path)
        for j in range(i0 + 1, min(len(frozen), i0 + (max(DELTAS) + 1) * S.STEPS_PER_HOUR + 1)):
            frozen[j] = frozen[i0]
        sim0.path = frozen
        r = sim0.mkt()
        h = sim0.v.h
        for dlt in DELTAS:
            for k in KS:
                base = branch_baseline(sim0, dlt, k)
                nav_b = base.v.nav(base.mkt_sqrt())
                pay_b, ch_b = pay(base), charges(base)
                arb_b, end_sqrt = base.arb_pnl, base.v.pool.s
                pushes = [(f"{int(p * 100)}%{'+' if sg > 0 else '-'}", int(round(r)) + sg * ticks_of(p), None)
                          for p in PUSHES for sg in (1, -1)]
                if variant == "exit" and h.L:
                    edge = sqrt_at(h.lo) + 1 if r < h.lo else sqrt_at(h.hi) - 1
                    pushes.append(("park", None, edge))
                for name, tt, ex in pushes:
                    att, sybil, ctl_extra, credited = branch_attack(sim0, tt, dlt, k, ex, end_sqrt)
                    dpay = pay(att) - pay_b
                    dnav = att.v.nav(att.mkt_sqrt()) - nav_b
                    net = (sybil - arb_b) + dpay + F_CAP * dnav
                    rows.append(dict(seed=seed, sigma=sigma, variant=variant, dlt=dlt, k=k, push=name,
                                     net=net, sybil=sybil, dpay=dpay, dnav=dnav, nav=nav_b,
                                     charge_attack=charges(att), charge_base=ch_b, credited=credited,
                                     ctl_net=net + ctl_extra, ctl_extra=ctl_extra))
    return rows


def main():
    os.makedirs(OUT, exist_ok=True)
    jobs = [(s, sg) for sg in SIGMAS for s in TEST_SEEDS]
    t0 = time.time()
    rows = []
    with mp.Pool(9) as pool:
        for r in pool.imap_unordered(run_path, jobs, chunksize=4):
            rows.extend(r)
    with open(os.path.join(OUT, "n1.json"), "w") as fh:
        json.dump(rows, fh)
    print(f"N1 (cell b): {len(rows)} rows, {time.time() - t0:.0f}s")
    ok = True
    grid = [r for r in rows if r["push"] != "park"]
    mx = max(grid, key=lambda r: r["net"])
    ch_ok = all(r["charge_attack"] == r["charge_base"] for r in rows)  # no action in the window: the charge is unchanged
    print(f"  assertion: max net over {len(grid)} grid rows = {mx['net']:.0f} wei ({mx['net'] * 1e4 / mx['nav']:.5f} bp; "
          f"{mx['push']} {mx['variant']} Δ={mx['dlt']} k={mx['k']}) -> {'PASS' if mx['net'] <= 1 else 'FAIL'}")
    print(f"  charge unchanged (attack == baseline, every row): {ch_ok}")
    ok &= mx["net"] <= 1 and ch_ok
    for p in sorted({r["push"] for r in grid}):
        g = [r for r in grid if r["push"] == p]
        print(f"    {p:5s}: max net {max(r['net'] for r in g) * 1e4 / g[0]['nav']:.4f} bp, "
              f"p50 {pctl([r['net'] * 1e4 / r['nav'] for r in g], 0.5):.4f} bp")
    park = [r for r in rows if r["push"] == "park"]
    if park:
        G = 300
        print(f"  parking lane (disclosed): {len(park)} rows; max τ_v credited {max(r['credited'] for r in park)} blocks "
              f"(bound 2·GRID = {2 * G}); max net {max(r['net'] for r in park) / ETH:.6f} ETH "
              f"({max(r['net'] * 1e4 / r['nav'] for r in park):.5f} bp); positive rows {sum(1 for r in park if r['net'] > 0)}")
        ok &= max(r["credited"] for r in park) <= 2 * G
    for sg in SIGMAS:
        best = {}
        for r in rows:
            if r["sigma"] == sg and r["k"] == 0:
                best[r["seed"]] = max(best.get(r["seed"], 0.0), max(0.0, r["ctl_net"] * 1e4 / r["nav"]))
        xs = [best.get(s, 0.0) for s in TEST_SEEDS]
        m, lo, _ = mean_ci(xs)
        print(f"  control (τ_v on the pool price alone) σ {sg}: mean {m:.6f} bp, CI lo {lo:.6f}, "
              f"profitable paths {sum(1 for x in xs if x > 0)}")
        ok &= lo > 0
    print("N1 verdict:", "PASS" if ok else "FAIL")
    return ok


if __name__ == "__main__":
    sys.exit(0 if main() else 1)
