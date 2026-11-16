#!/usr/bin/env python3
"""Closed-form re-gate rows for the markout-settled charge: L6 (holding the reference pool over the markout window)
and L11 (solvency with the reserve). Seed-free.

L6. A colluder that waited for a market move of D ticks past the vault's priced legs acts, then holds the reference
pool's closing tick d ticks off the market in j of the window's ten 12-second blocks, shifting the markout average
by (j/10)·d. Gain per attempt (charge avoided; c ≈ 2.6e-8 NAV per tick², one fee margin): 2c·NAV·D·(w·d − FEE_TICKS)
− c·NAV·((w·d)² + Var η), w = j/10, Var η of the window at σ 120%. Cost under competitive block building: every held
block costs a round trip of fees on the notional that moves the reference pool d ticks at its liquidity L_deep
(observed 4.55e18; stress L_deep/4). The safe NAV is the largest NAV at which the best (D, d) has gain ≤ cost, for
every j. The row passes iff that NAV ≥ NAV_MAX at L_deep/4. Reported: a builder that holds all ten blocks and unwinds
once (one round trip in total), outside the admitted threat model. Control: L_deep → 0 (no NAV is safe).

L11. The worst action of each type at REF_BAND 60 (the G0 solvency search, output/g0/solvency.json) with the
reserve: the gate (burn + burnR ≤ unvested + bond) at BOND_RATIO·S with no unvested pay must pass; control: at half
of (worst + reserve) the gate fails. Reported: the coverage multiple BOND_RATIO / (worst + RESERVE_RATIO) (the
specification's sizing rule asks for ≥ 2) and the cover left after one fallback settlement.
"""
import json
import os
import sys

import numpy as np

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
from common import R  # noqa: E402

ROOT = os.path.dirname(os.path.dirname(HERE))
C, FEE_T, NB = 2.6e-8, 5, 10
SQRT_P = 1.856032392481742725e33 / 2 ** 96
NAV_MAX = 1850
BOND_RATIO = R.MAINNET.BOND_RATIO / 1e18
RESERVE = R.MAINNET.RESERVE_RATIO / 1e18


def sd12(s):
    return s * np.sqrt(12 / (365 * 86400)) / np.log(1.0001)


D_ = np.arange(10, 181, 10)[:, None]
d_ = np.arange(1, 600, 3)[None, :]


def safe_nav(ls, j, per_block=True, sigma=1.2, l_deep=4.55e18):
    w = j / NB
    per_tick = l_deep * ls * SQRT_P * 5e-5 / 1e18
    var = sd12(sigma) ** 2 * (1 + NB / 3)
    last = None
    for nav in range(50, 30001, 50):
        cn = C * nav
        cost = (j if per_block else 1) * 2 * 5e-4 * per_tick * d_
        best = (2 * cn * D_ * (w * d_ - FEE_T) - cn * ((w * d_) ** 2 + var) - cost).max()
        if best <= 0:
            last = nav
        else:
            break
    return last


def l6():
    lines = ["L6 (closed form, σ 120%, H 120 s, window delayed one block, one fee margin):"]
    res = {}
    for ls in (1.0, 0.25):
        per = {j: safe_nav(ls, j) for j in range(1, NB + 1)}
        res[ls] = min(v for v in per.values())
        lines.append(f"  competitive building, L_deep ×{ls}: safe NAV over j = 1..10 held blocks: min {res[ls]} WETH "
                     f"(j=1 {per[1]}, j=10 {per[10]}); builder holding all ten blocks, one round trip (disclosed limit): "
                     f"{safe_nav(ls, NB, per_block=False)} WETH")
    global FEE_T
    f0, FEE_T = FEE_T, FEE_T - 1      # the markout tick is floored on chain (integer closes, a floored mean): ≈ 1 tick lost
    fl = {ls: min(safe_nav(ls, j) for j in range(1, NB + 1)) for ls in (1.0, 0.25)}
    FEE_T = f0
    lines.append(f"  with the on-chain floor of the markout tick (margin FEE_TICKS − 1): safe NAV {fl[1.0]} / {fl[0.25]} WETH "
                 f"(L_deep / L_deep/4): {'meets' if fl[0.25] >= NAV_MAX else 'BELOW'} NAV_MAX {NAV_MAX}")
    # v2.34 (owner R1-02): the upper leg at floor + FEE_TICKS + 1 >= ceil + FEE_TICKS, so its margin is >= FEE_TICKS again
    # (the floor only widens the lower leg's margin): the full-margin row applies.
    up = {ls: min(safe_nav(ls, j) for j in range(1, NB + 1)) for ls in (1.0, 0.25)}
    lines.append(f"  with the upper leg rounded up (floor + FEE_TICKS + 1, spec v2.34): margin >= FEE_TICKS, safe NAV "
                 f"{up[1.0]} / {up[0.25]} WETH: {'meets' if up[0.25] >= NAV_MAX else 'BELOW'} NAV_MAX {NAV_MAX}")
    ctl = safe_nav(1.0, 1, l_deep=1.0)
    ok = res[0.25] is not None and res[0.25] >= NAV_MAX
    lines.append(f"  rule: safe NAV ≥ NAV_MAX {NAV_MAX} at L_deep/4: {'PASS' if ok else 'FAIL'}; control L_deep → 0: "
                 f"safe NAV {ctl} (none ⇒ the colluder profits at every NAV)")
    return ok, "\n".join(lines)


def l11():
    sol = json.load(open(os.path.join(ROOT, "output", "g0", "solvency.json")))
    S = 10 ** 24
    lines = [f"L11 (solvency with the reserve; BOND_RATIO {BOND_RATIO:.3e}, RESERVE_RATIO {RESERVE:.1e}, no unvested pay):"]
    ok = True
    for typ in ("swap", "place"):
        case = sol["bands"]["60"][f"{typ}_case"]
        b, a = R.Holdings(**case["before"]), R.Holdings(**case["after"])
        sp, sr = int(case["sqrtSpot"]), int(case["sqrtRef"])
        res = R.mul_div(R.MAINNET.RESERVE_RATIO, R.value(b, sp)[0], R.WAD)
        c, p, n, burn, burn_r, _ = R.measure_c(b, a, sp, sp, sr, res, S, 0, 0)
        bond = S * R.MAINNET.BOND_RATIO // R.WAD
        passes = burn + burn_r <= bond
        need = burn + burn_r
        worst = sol["bands"]["60"][typ]
        half_fails = not (need <= int(S * (worst + RESERVE) / 2))   # control: a bond of half the worst action
        mult = BOND_RATIO / (worst + RESERVE)
        left = (bond - need) / S
        ok = ok and passes and half_fails
        lines.append(f"  worst {typ}: immediate {worst:.4e} + reserve {RESERVE:.1e} = {worst + RESERVE:.4e} of NAV; gate at "
                     f"BOND_RATIO {'passes' if passes else 'FAILS'}; control at half fails: {half_fails}; coverage "
                     f"{mult:.3f}× (sizing rule ≥ 2×); cover left after this action and a fallback {left:.3e} of supply")
    lines.append(f"  rule: the gate covers the worst action: {'PASS' if ok else 'FAIL'}")
    return ok, "\n".join(lines)


if __name__ == "__main__":
    o6, t6 = l6()
    o11, t11 = l11()
    s = t6 + "\n" + t11
    print(s)
    out = os.path.join(ROOT, "output", "s5")
    os.makedirs(out, exist_ok=True)
    with open(os.path.join(out, "regate_rows.txt"), "w") as fh:
        fh.write(s + "\n")
