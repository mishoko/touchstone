#!/usr/bin/env python3
"""An honest re-placement settled under the fee-aware rule (`R.settle_extra`) and under the holdings-only rule.

The vault holds a position on [-1000, 1000) at tick 0 (18/18-decimal tokens, both sides 1,000e18), the market and the
reference at tick 0. The curator re-places at the market without a move, shifting the range by +200 or +600 ticks
(and, as controls, by 0 and -200). The markout settles at a window tick of +12 (a rise), +60 or -60. Reported per case:
the immediate charge, the settlement burn burnS as a fraction of the supply S_act and of the reserve's shares, and the
holdings-only rule's burn (legs tick_mk ± FEE_TICKS, with and without the upper leg rounded up).

Usage: python3 model/cells/f046_honest.py
"""
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
from common import R, sqrt_at  # noqa: E402
from sim import settle_holdings_only, _ORIG_SETTLE  # noqa: E402

FEE, FEE_TICKS, W = 500, 5, 2000
S_ACT = 10 ** 27
RESERVE_RATIO = R.MAINNET.RESERVE_RATIO


def case(shift, tick_mk):
    s = sqrt_at(0)
    idle = R.Holdings(1000 * 10 ** 18, 1000 * 10 ** 18)
    _, before, _ = R.placement_size(idle, -1000, 1000, s, s)
    x, y = R.position_amounts(before.L, before.lo, before.hi, s)
    removed = R.Holdings(before.idle0 + x, before.idle1 + y)
    _, after, _ = R.placement_size(removed, -1000 + shift, 1000 + shift, s, s)
    c0, _, _, burn0, burn_r, _ = R.measure_c(before, after, s, s, s, R.mul_div(RESERVE_RATIO, R.value(before, s)[0], R.WAD),
                                                     S_ACT, 2 ** 127, 2 ** 127)
    reserve = R.mul_div(RESERVE_RATIO, R.value(before, s)[0], R.WAD)
    pend = dict(s0=s, sEnd=s, charge0=c0, reserve=reserve, burn0=burn0, sAct=S_ACT)
    ex_fa, _, b_fa = _ORIG_SETTLE(before, after, pend, tick_mk, FEE_TICKS, FEE, burn_r)
    S_now = S_ACT - burn0
    _, _, b_ho = settle_holdings_only(before, after, tick_mk, FEE_TICKS, c0, reserve, after, S_now, burn_r, False)
    _, _, b_ho1 = settle_holdings_only(before, after, tick_mk, FEE_TICKS, c0, reserve, after, S_now, burn_r, True)
    return dict(shift=shift, tick=tick_mk, c0=c0, burn0=burn0, b_fa=b_fa, ex_fa=ex_fa, b_ho=b_ho, b_ho1=b_ho1, rs=burn_r,
                Lb=before.L, La=after.L, nav=R.value(before, s)[0])


def main():
    print(f"honest re-placement at tick 0, no move; S_act = {S_ACT:.0e}; reserve ratio {RESERVE_RATIO / 1e18:g}")
    print(f"{'shift':>6} {'tick_mk':>7} {'charge0':>8} {'burnS/S (fee-aware)':>14} {'of reserve':>11} {'extra/NAV':>10} "
          f"{'burnS/S (holdings)':>14} {'(+1 leg)':>10}  L_after/L_before")
    for shift in (200, 600, 0, -200):
        for t in (12, 60, -60):
            c = case(shift, t)
            print(f"{c['shift']:>+6} {c['tick']:>+7} {c['c0']:>8} {c['b_fa'] / S_ACT:>14.3e} {c['b_fa'] / c['rs']:>11.3e} "
                  f"{c['ex_fa'] / c['nav']:>10.2e} {c['b_ho'] / S_ACT:>14.3e} {c['b_ho1'] / S_ACT:>10.3e}  {c['La'] / c['Lb']:.4f}")


if __name__ == "__main__":
    main()
