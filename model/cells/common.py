"""Shared machinery for the Python attack and solvency cells.

Pre-registered rules: an attack cell passes if its maximum net over all paths is <= 0 (rounding
tolerance per action) and its positive control is > 0 with the one-sided 95% CI lower bound > 0; >= 200 paths
per sigma bucket (40/60/120%/yr); seeds 0-99 fit, seeds 100-299 test.
"""
import math
import os
import random
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(HERE))
import reference as R  # noqa: E402
from v4math import get_sqrt_price_at_tick as sqrt_at, get_tick_at_sqrt_price  # noqa: E402

T0 = 196250            # tier A orientation: token0 USDC (6 dp), token1 WETH (18 dp)
USDC, ETH = 10 ** 6, 10 ** 18
SIGMAS = (0.40, 0.60, 1.20)
TEST_SEEDS = range(100, 300)
HOURS_PER_YEAR = 365 * 24
W = 2000
SPACING = 10


def floor_sp(t):
    return t // SPACING * SPACING


def gbm_ticks(seed, sigma, hours=24 * 30):
    """Hourly log-price path in ticks (driftless GBM)."""
    rng = random.Random(seed * 1000 + int(sigma * 100))
    sd = sigma / math.sqrt(HOURS_PER_YEAR) / math.log(1.0001)
    t, out = float(T0), []
    for _ in range(hours):
        t += rng.gauss(0.0, sd)
        out.append(int(round(t)))
    return out, rng


def edge60_range(idle, ref, spot):
    """The edge60 range classification: mostly token0 ⇔ q(idle0, ref) >= idle1; [c, c+W) or [c'+spacing−W, c'+spacing)."""
    if R.q(idle.idle0, sqrt_at(ref)) >= idle.idle1:
        c = floor_sp(min(ref, spot))
        return c, c + W
    c = floor_sp(max(ref, spot))
    return c + SPACING - W, c + SPACING


def sample_state(seed, sigma, nav_eth=1000, exit_state=False):
    """A vault state drawn from an edge60-managed path: holdings (with position) and the market tick.
    The manager re-places at the edge on its inventory side after one hour out of range; fees are ignored
    (they only scale holdings)."""
    path, rng = gbm_ticks(seed, sigma)
    tick = path[0]
    s = sqrt_at(tick)
    half = nav_eth * ETH // 2
    idle = R.Holdings(R.mul_div(half, 1 << 192, s * s), half)
    lo = floor_sp(tick) - W // 2
    L, h, _ = R.placement_size(idle, lo, lo + W, s, s)
    out_for = 0
    stop = rng.randrange(len(path) // 3, len(path))
    for i, t in enumerate(path[:stop] if not exit_state else path):
        if h.lo <= t < h.hi:
            out_for = 0
            continue
        if exit_state and i >= stop:
            return h, t, rng  # the first hour out of range after `stop`: the vault is out of range at t
        out_for += 1
        if out_for >= 1:  # EDGE_DELAY = 1 GRID = 1 hour
            st = sqrt_at(t)
            x, y = R.position_amounts(h.L, h.lo, h.hi, st)
            idle2 = R.Holdings(h.idle0 + x, h.idle1 + y)
            lo2, hi2 = edge60_range(idle2, t, t)
            L2, h2, d = R.placement_size(idle2, lo2, hi2, st, st)
            if d < R.WAD // 2:  # move spot one unit inside the inventory-side edge (empty region), place
                se = sqrt_at(lo2) + 1 if lo2 == floor_sp(t) else sqrt_at(hi2) - 1
                L2, h2, d = R.placement_size(idle2, lo2, hi2, se, st)
            if d >= R.WAD // 2 and L2 > 0:
                h = h2
            out_for = 0
    return h, path[stop - 1], rng


def mean_ci(xs):
    n = len(xs)
    m = sum(xs) / n
    if n < 2:
        return m, m, m
    var = sum((x - m) ** 2 for x in xs) / (n - 1)
    se = math.sqrt(var / n)
    return m, m - 1.645 * se, m + 1.645 * se  # one-sided 95% bounds


def pctl(xs, p):
    ys = sorted(xs)
    k = min(len(ys) - 1, max(0, int(math.ceil(p * len(ys))) - 1))
    return ys[k]
