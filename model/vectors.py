"""Golden-vector emitter for the Touchstone reference model.

Writes `test/vectors/{math,ledger,bank,placement,clock,mode,scenario}.json`, each
`{schema, specVersion, conventions, cases:[{id, fn, in, out}]}` with every integer as a decimal string.
Action vectors take holdings before and after as inputs; the holdings are produced here by executing
the action against the model's pool simulator, so they are realistic, but the vectors themselves need
no swap math to check.
"""
import copy
import json
import os
import random

import reference as R
from v4math import (MIN_TICK, MAX_TICK, MIN_SQRT_PRICE, MAX_SQRT_PRICE, MathRevert, Q96, U256, mul_div,
                    mul_div_up, get_sqrt_price_at_tick as sqrt_at, get_tick_at_sqrt_price, get_amount0_delta,
                    get_amount1_delta, get_liquidity_for_amounts)

OUT = os.environ.get("VECTORS_OUT") or os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "test", "vectors")
SCHEMA = "touchstone-vectors/1"
MIN_CASES = {"math": 40, "ledger": 10, "bank": 30, "placement": 10, "clock": 30, "mode": 30, "scenario": 30}

T0 = 196250  # tier A orientation: token0 USDC (6 decimals), token1 WETH (18 decimals); ≈ 3,000 USDC/WETH
USDC = 10 ** 6
ETH = 10 ** 18


def enc(o):
    if isinstance(o, bool) or o is None:
        return o
    if isinstance(o, int):
        return str(o)
    if isinstance(o, dict):
        return {k: enc(v) for k, v in o.items()}
    if isinstance(o, (list, tuple)):
        return [enc(v) for v in o]
    if isinstance(o, set):
        return sorted(o)
    return o


def write(name, conventions, cases):
    assert len(cases) >= MIN_CASES[name], (name, len(cases))
    ids = [c["id"] for c in cases]
    assert len(ids) == len(set(ids)), "duplicate vector id"
    doc = dict(schema=SCHEMA, specVersion=R.SPEC_VERSION, conventions=conventions, cases=enc(cases))
    os.makedirs(OUT, exist_ok=True)
    with open(os.path.join(OUT, f"{name}.json"), "w") as fh:
        json.dump(doc, fh, indent=1)
        fh.write("\n")
    return len(cases)


UPSTREAM_ERR = {"mulDiv": "", "mulDivRoundingUp": "", "divRoundingUp": "", "ceilDiv": ""}  # empty revert data


def guard(key, fn, *a):
    """out = {key: value}, or {revert: <upstream error name>} ("" = a revert with empty data)."""
    try:
        return {key: fn(*a)}
    except MathRevert as ex:
        name = str(ex).split(":")[0]
        return {"revert": UPSTREAM_ERR.get(name, name)}


# ======================================================================== math
def math_cases():
    rng = random.Random(1001)
    cases = []

    def add(fn, i, o):
        cases.append(dict(id=f"math-{len(cases):03d}", fn=fn, **{"in": i, "out": o}))

    ticks = [MIN_TICK, MIN_TICK + 1, -200000, -1, 0, 1, 10, T0, T0 + 10, MAX_TICK - 1, MAX_TICK]
    ticks += [rng.randint(MIN_TICK, MAX_TICK) for _ in range(12)]
    for t in ticks:
        add("TickMath.getSqrtPriceAtTick", {"tick": t}, {"sqrtPriceX96": sqrt_at(t)})
    add("TickMath.getSqrtPriceAtTick", {"tick": MAX_TICK + 1}, {"revert": "InvalidTick"})
    for t in [MIN_TICK, -1, 0, T0, MAX_TICK - 1]:
        s = sqrt_at(t)
        add("TickMath.getTickAtSqrtPrice", {"sqrtPriceX96": s}, {"tick": t})
        if s > MIN_SQRT_PRICE:
            add("TickMath.getTickAtSqrtPrice", {"sqrtPriceX96": s - 1}, {"tick": get_tick_at_sqrt_price(s - 1)})
    for _ in range(6):
        s = rng.randint(MIN_SQRT_PRICE, MAX_SQRT_PRICE - 1)
        add("TickMath.getTickAtSqrtPrice", {"sqrtPriceX96": s}, {"tick": get_tick_at_sqrt_price(s)})
    add("TickMath.getTickAtSqrtPrice", {"sqrtPriceX96": MAX_SQRT_PRICE}, {"revert": "InvalidSqrtPrice"})
    pairs = [(sqrt_at(T0 - 1000), sqrt_at(T0 + 1000)), (sqrt_at(-50), sqrt_at(70)), (MIN_SQRT_PRICE, sqrt_at(-887000)),
             (sqrt_at(887000), MAX_SQRT_PRICE)]
    for sa, sb in pairs:
        for L in [1, 10 ** 12, 10 ** 24, (1 << 128) - 1]:
            for up in (False, True):
                add("SqrtPriceMath.getAmount0Delta", dict(sqrtPriceAX96=sb, sqrtPriceBX96=sa, liquidity=L, roundUp=up),
                    guard("amount", get_amount0_delta, sa, sb, L, up))
                add("SqrtPriceMath.getAmount1Delta", dict(sqrtPriceAX96=sa, sqrtPriceBX96=sb, liquidity=L, roundUp=up),
                    guard("amount", get_amount1_delta, sa, sb, L, up))
    sa, sb = sqrt_at(T0 - 1000), sqrt_at(T0 + 1000)
    for s in [sa - 1, sa, sqrt_at(T0), sb - 1, sb, sb + 1]:
        for a0, a1 in [(10 ** 12, 5 * 10 ** 20), (0, 10 ** 20), (10 ** 12, 0), (1, 1)]:
            add("LiquidityAmounts.getLiquidityForAmounts",
                dict(sqrtPriceX96=s, sqrtPriceAX96=sa, sqrtPriceBX96=sb, amount0=a0, amount1=a1),
                guard("liquidity", get_liquidity_for_amounts, s, sa, sb, a0, a1))
    for a, b, d in [(U256, U256, U256), (1, 1, 3), (2 ** 200, 2 ** 100, 2 ** 150 + 7), (7, 11, 13), (U256, 2, 1)]:
        add("FullMath.mulDiv", dict(a=a, b=b, denominator=d), guard("result", mul_div, a, b, d))
        add("FullMath.mulDivRoundingUp", dict(a=a, b=b, denominator=d), guard("result", mul_div_up, a, b, d))
    for s in [sqrt_at(T0), sqrt_at(-T0), (1 << 128) - 1, 1 << 128, MAX_SQRT_PRICE - 1, MIN_SQRT_PRICE]:
        for x in [0, 1, 10 ** 12, 10 ** 30]:
            add("LiquidityValue.q", dict(x=x, sqrtPriceX96=s), guard("value", R.q, x, s))
    L = 3 * 10 ** 17
    for s in [sqrt_at(T0 - 1000) - 1, sqrt_at(T0 - 1000), sqrt_at(T0), sqrt_at(T0 + 1000) - 1, sqrt_at(T0 + 1000)]:
        x, y = R.position_amounts(L, T0 - 1000, T0 + 1000, s)
        add("LiquidityValue.amounts", dict(L=L, lo=T0 - 1000, hi=T0 + 1000, sqrtPriceX96=s), dict(amount0=x, amount1=y))
    return cases


MATH_CONV = {
    "ports": "TickMath, SqrtPriceMath, FullMath (v4-core) and LiquidityAmounts (v4-periphery) with their roundUp flags.",
    "LiquidityValue.q": "q(x,s) = s <= 2^128-1 ? mulDiv(s*s, x, 2^192) : mulDiv(mulDiv(s,s,2^64), x, 2^128).",
    "LiquidityValue.amounts": "Appendix A at sqrt price s with removal rounding (roundUp = false): s <= sa all token0; "
                              "s >= sb all token1; else getAmount0Delta(s, sb) and getAmount1Delta(sa, s).",
    "revert": "out = {revert: <error name>} when the upstream function reverts; \"\" means a revert with empty "
              "data (FullMath). Error names: InvalidTick, InvalidSqrtPrice, InvalidPrice, SafeCastOverflow.",
    "fields": "TickMath.getSqrtPriceAtTick in {tick} out {sqrtPriceX96}; TickMath.getTickAtSqrtPrice in {sqrtPriceX96} "
              "out {tick}; SqrtPriceMath.getAmount0Delta/getAmount1Delta in {sqrtPriceAX96, sqrtPriceBX96, liquidity, "
              "roundUp} out {amount}; LiquidityAmounts.getLiquidityForAmounts in {sqrtPriceX96, sqrtPriceAX96, "
              "sqrtPriceBX96, amount0, amount1} out {liquidity}; FullMath.mulDiv/mulDivRoundingUp in {a, b, "
              "denominator} out {result}; LiquidityValue.q in {x, sqrtPriceX96} out {value}; LiquidityValue.amounts "
              "in {L, lo, hi, sqrtPriceX96} out {amount0, amount1}.",
}


# ======================================================================== ledger
def _pool_with(tick=T0, third_L=0, third_halfwidth=4000, fee=500):
    p = R.Pool(sqrt_at(tick), fee)
    if third_L:
        lo = (tick - third_halfwidth) // 10 * 10
        p.modify("lp", lo, lo + 2 * third_halfwidth, third_L)
    return p


def _place(pool, h, lo, hi, s_ref):
    """Remove the old position to idle, then size and add the new one at spot (no pre-swap)."""
    h = h.copy()
    if h.L:
        a0, a1, f0, f1 = pool.modify("vault", h.lo, h.hi, -h.L)
        h.idle0 += -a0 + f0
        h.idle1 += -a1 + f1
        h.L = 0
    L, after, d = R.placement_size(h, lo, hi, pool.s, s_ref)
    if L:
        pool.modify("vault", lo, hi, L)
    return after, d


def ledger_cases():
    cases = []
    S, unv, bond = 10 ** 24, 4 * 10 ** 20, 5 * 10 ** 20

    def add(tag, before, after, s_spot, s_ref, S_=S, unv_=unv, bond_=bond, note="", expect=None, s0=None, reserve=0):
        s0 = s_spot if s0 is None else s0
        c, p, n, b, br, ok = R.measure_c(before, after, s0, s_spot, s_ref, reserve, S_, unv_, bond_)
        if expect:  # the generator asserts that each vector shows what its name says
            leg, positive, ok_want = expect
            assert (p == {"spot": s_spot, "ref": s_ref, "s0": s0}[leg]), (tag, "p*")
            assert (c > rounding(s_spot)) == positive, (tag, "charge", c)
            assert ok == ok_want, (tag, "ok")
        cases.append(dict(id=f"ledger-{len(cases):03d}-{tag}", fn="ExecLedger.measure",
                          **{"in": dict(before=before.d(), after=after.d(), sqrtS0=s0, sqrtSpot=s_spot, sqrtRef=s_ref,
                                        reserve=reserve, S=S_, unvested=unv_, bond=bond_),
                             "out": dict(charge=c, pStar=p, navAfterPStar=n, burn=b, burnR=br, ok=ok)}))

    def adds(tag, before, after, tick_mk, charge0, reserve, now_h, S_, rshares, note=""):
        ex, pm, bs = R.settle_extra(before, after, tick_mk, 5, charge0, reserve, now_h, S_, rshares)
        cases.append(dict(id=f"ledger-{len(cases):03d}-{tag}", fn="ExecLedger.settleExtra",
                          **{"in": dict(before=before.d(), after=after.d(), tickMk=tick_mk, feeTicks=5, charge0=charge0,
                                        reserve=reserve, now=now_h.d(), S=S_, reserveShares=rshares),
                             "out": dict(extra=ex, pMk=pm, burnS=bs)}))

    def addv(tag, h, s):
        try:
            nav, pv = R.value(h, s)
        except MathRevert as ex:
            cases.append(dict(id=f"ledger-{len(cases):03d}-{tag}", fn="ExecLedger.value",
                              **{"in": dict(h=h.d(), sqrtP=s), "out": {"revert": UPSTREAM_ERR.get(str(ex).split(":")[0], str(ex))}}))
            return
        cases.append(dict(id=f"ledger-{len(cases):03d}-{tag}", fn="ExecLedger.value",
                          **{"in": dict(h=h.d(), sqrtP=s), "out": dict(nav=nav, posValue=pv)}))

    def rounding(sp):
        return R.q(3, sp) + 3

    def swap(pool, h, zfo, amount, lim_tick):
        used, out = pool.swap(zfo, amount, sqrt_at(lim_tick))
        a = h.copy()
        if zfo:
            a.idle0 -= used
            a.idle1 += out
        else:
            a.idle1 -= used
            a.idle0 += out
        if a.L:
            _, _, f0, f1 = pool.modify("vault", a.lo, a.hi, 0)
            a.idle0 += f0
            a.idle1 += f1
        return a

    idle = R.Holdings(1_500_000 * USDC, 500 * ETH)
    # 1. zero action (no-op): charge exactly 0, p* = spot
    pool = _pool_with(third_L=10 ** 18)
    h, _ = _place(pool, idle, T0 - 1000, T0 + 1000, sqrt_at(T0))
    add("zero-action", h, h.copy(), pool.s, sqrt_at(T0), note="no-op action: charge 0", expect=("spot", False, True))
    # 2. re-range at spot = reference: value-neutral at spot (rounding only)
    pool = _pool_with(third_L=10 ** 18)
    h0, _ = _place(pool, idle, T0 - 1000, T0 + 1000, sqrt_at(T0))
    h1, _ = _place(pool, h0, T0 - 1500, T0 + 500, sqrt_at(T0))
    add("rerange-at-ref", h0, h1, pool.s, sqrt_at(T0), note="no-swap re-range, spot = reference: rounding only",
        expect=("spot", False, True))
    # 3. pushed spot: spot pushed +60 ticks above the reference; an idle (mostly token1) vault places the
    #    token1-side edge range [c'+10-W, c'+10) at the pushed spot; the reference leg prices the curvature
    pool = _pool_with(third_L=10 ** 17)
    pool.push_to(sqrt_at(T0 + 60))
    h0 = R.Holdings(20_000 * USDC, 600 * ETH)
    h1, d = _place(pool, h0, T0 + 70 - 2000, T0 + 70, sqrt_at(T0))
    add("pushed-spot", h0, h1, pool.s, sqrt_at(T0), note=f"spot +60; edge placement; p* = ref (deltaAfter {d})",
        expect=("ref", True, True))
    # 4. lagging reference: spot = market at T0; the reference lags 40 ticks below; token0-side edge placement
    pool = _pool_with(third_L=10 ** 18)
    h0 = R.Holdings(3_000_000 * USDC, 30 * ETH)
    h1, d = _place(pool, h0, T0 - 40, T0 - 40 + 2000, sqrt_at(T0 - 40))
    add("lagging-ref", h0, h1, pool.s, sqrt_at(T0 - 40), note=f"reference 40 ticks below spot (deltaAfter {d})",
        expect=("ref", True, True))
    # 5. swaps from idle: 2% of NAV token0 -> token1, deep and thin third-party depth, spot = reference
    for tag, third in (("swap-deep", 10 ** 20), ("swap-thin", 10 ** 17)):
        pool = _pool_with(third_L=third)
        h0 = idle.copy()
        amt = R.mul_div(R.value(h0, sqrt_at(T0))[0] * 2 // 100, 1 << 192, sqrt_at(T0) ** 2)
        h1 = swap(pool, h0, True, amt, T0 - 10)
        add(tag, h0, h1, sqrt_at(T0), sqrt_at(T0), note="rebalanceSwap 2% NAV, limit 10 ticks: fee + impact",
            expect=("spot", True, True))
    # 6. swap with the vault's own position in range: impact through its own liquidity is charged
    pool = _pool_with(third_L=10 ** 17)
    h0, _ = _place(pool, R.Holdings(3_000_000 * USDC, 500 * ETH), T0 - 1000, T0 + 1000, sqrt_at(T0))
    amt = min(h0.idle0, R.mul_div(R.value(h0, sqrt_at(T0))[0] * 2 // 100, 1 << 192, sqrt_at(T0) ** 2))
    h1 = swap(pool, h0, True, amt, T0 - 10)
    add("swap-own-liquidity", h0, h1, sqrt_at(T0), sqrt_at(T0), note="own position in range collects part of the fee",
        expect=("spot", True, True))
    # 7. swap at the fence, selling token0 at a spot 60 ticks above the reference, JIT at the limit
    pool = _pool_with(third_L=0)
    lim = T0 + 50
    pool.modify("jit", lim, lim + 10, 10 ** 22)
    pool.push_to(sqrt_at(T0 + 60))
    h0 = idle.copy()
    amt = R.mul_div(R.value(h0, sqrt_at(T0))[0] * 2 // 100, 1 << 192, sqrt_at(T0) ** 2)
    h1 = swap(pool, h0, True, amt, lim)
    add("swap-fence-sell", h0, h1, sqrt_at(T0 + 60), sqrt_at(T0), note="spot +60, JIT at the limit; spot leg worse",
        expect=("spot", True, True))
    # 8. swap at the fence, buying token0 at a spot 60 ticks above the reference: the reference leg is worse
    pool = _pool_with(third_L=0)
    pool.modify("jit", T0 + 60, T0 + 70, 10 ** 22)
    pool.push_to(sqrt_at(T0 + 60))
    h1 = swap(pool, h0, False, h0.idle1 * 2 // 100, T0 + 70)
    add("swap-fence-buy", h0, h1, sqrt_at(T0 + 60), sqrt_at(T0), note="buy token0 60 ticks above the reference",
        expect=("ref", True, True))
    # 9. edge placement (edge60), token0 side, reference 15 ticks below spot
    pool = _pool_with(third_L=10 ** 18, tick=T0 + 35)
    h0 = R.Holdings(3_000_000 * USDC, 2 * ETH)
    c = (min(T0 + 35, T0 + 20)) // 10 * 10
    h1, d = _place(pool, h0, c, c + 2000, sqrt_at(T0 + 20))
    add("edge-placement", h0, h1, pool.s, sqrt_at(T0 + 20), note=f"edge60 token0 side (deltaAfter {d})",
        expect=("ref", True, True))
    # 10. gate revert: the pushed-spot charge against a tiny bond and no unvested pay
    pool = _pool_with(third_L=10 ** 17)
    pool.push_to(sqrt_at(T0 + 60))
    g0 = R.Holdings(20_000 * USDC, 600 * ETH)
    g1, _ = _place(pool, g0, T0 + 70 - 2000, T0 + 70, sqrt_at(T0))
    add("gate-revert", g0, g1, pool.s, sqrt_at(T0), unv_=0, bond_=10 ** 15,
        note="burn > unvested + bond -> ok = false (Insolvent)", expect=("ref", True, False))
    add("gate-exact", g0, g1, pool.s, sqrt_at(T0), unv_=0,
        bond_=R.measure(g0, g1, pool.s, sqrt_at(T0), S, 0, 0)[3], note="burn == unvested + bond -> ok",
        expect=("ref", True, True))
    burn_g = R.measure(g0, g1, pool.s, sqrt_at(T0), S, 0, 0)[3]
    add("gate-passes-on-unvested", g0, g1, pool.s, sqrt_at(T0), unv_=burn_g - burn_g // 3, bond_=burn_g // 3,
        note="bond < burn <= unvested + bond", expect=("ref", True, True))
    add("one-wei-loss", R.Holdings(10 ** 12, 10 ** 21), R.Holdings(10 ** 12, 10 ** 21 - 1), sqrt_at(T0), sqrt_at(T0),
        note="a 1-wei loss is charged 1 wei", expect=("spot", False, True))
    # 11. gain at both prices: the 0 floor
    g = h0.copy()
    g.idle1 += 10 ** 15
    add("gain-floor", h0, g, sqrt_at(T0), sqrt_at(T0 + 30), note="NAV rises at both prices: charge 0",
        expect=("spot", False, True))
    # 12. tie: spot == reference
    t = h0.copy()
    t.idle1 -= 10 ** 15
    add("tie-spot-ref", h0, t, sqrt_at(T0), sqrt_at(T0), note="equal losses: p* = sqrtSpot", expect=("spot", True, True))
    # 13. degenerate: everything lost, NAV_after(p*) = 0
    add("nav-after-zero", R.Holdings(10, 10), R.Holdings(), sqrt_at(T0), sqrt_at(T0),
        note="burn saturates at type(uint256).max; ok = false")
    # 14. burn rounding: ceil with a small S
    add("burn-ceil", h0, t, sqrt_at(T0), sqrt_at(T0), S_=3, unv_=1, bond_=1, note="ceilDiv(charge·S, NAV')")
    # 15. out-of-range position, no-op
    pool = _pool_with(third_L=10 ** 18)
    h0, _ = _place(pool, idle, T0 + 500, T0 + 2500, sqrt_at(T0))
    add("out-of-range-noop", h0, h0.copy(), sqrt_at(T0), sqrt_at(T0 + 60), note="position above spot; no-op",
        expect=("spot", False, True))
    # repair C: the pre-move spot leg s0, the reserve in the gate, and the settlement's markout legs
    pool = _pool_with(third_L=10 ** 17)
    pool.push_to(sqrt_at(T0 + 60))
    g0 = R.Holdings(20_000 * USDC, 600 * ETH)
    g1, _ = _place(pool, g0, T0 + 70 - 2000, T0 + 70, sqrt_at(T0 + 40))
    add("s0-leg-wins", g0, g1, pool.s, sqrt_at(T0 + 40), s0=sqrt_at(T0 - 30), note="the pre-move spot prices the action",
        expect=("s0", True, True))
    add("s0-equal-spot-tie", R.Holdings(10 ** 21, 10 ** 21), R.Holdings(10 ** 21, 10 ** 21 - 10 ** 15), sqrt_at(T0), sqrt_at(T0), s0=sqrt_at(T0), note="spot, s0 and ref tie: p* = sqrtSpot",
        expect=("spot", True, True))
    c_, p_, n_, b_, br_, _ = R.measure_c(g0, g1, sqrt_at(T0 - 30), pool.s, sqrt_at(T0 + 40), 10 ** 17, S, 0, 0)
    tie0, tie1 = R.Holdings(10 ** 21, 10 ** 21), R.Holdings(10 ** 21, 10 ** 21 - 10 ** 15)
    add("tie-distinct-prices", tie0, tie1, sqrt_at(T0 + 20), sqrt_at(T0 - 20), s0=sqrt_at(T0 + 50), reserve=10 ** 15,
        note="a token1-only loss ties every leg at distinct prices: p* = sqrtSpot", expect=("spot", True, True))
    add("reserve-gate-exact", g0, g1, pool.s, sqrt_at(T0 + 40), s0=sqrt_at(T0 - 30), reserve=10 ** 17, unv_=0,
        bond_=b_ + br_, note="burn + burnR == unvested + bond -> ok", expect=("s0", True, True))
    add("reserve-gate-revert", g0, g1, pool.s, sqrt_at(T0 + 40), s0=sqrt_at(T0 - 30), reserve=10 ** 17, unv_=0,
        bond_=b_ + br_ - 1, note="the reserve's shares fail the gate (Insolvent)", expect=("s0", True, False))
    add("reserve-charge-zero", R.Holdings(10 ** 21, 10 ** 21), R.Holdings(10 ** 21, 10 ** 21), sqrt_at(T0), sqrt_at(T0), reserve=6 * 10 ** 17,
        note="charge 0: burn 0, burnR = ceilDiv(reserve·S, NAV')", expect=("spot", False, True))
    add("reserve-nav-zero", R.Holdings(10, 10), R.Holdings(), sqrt_at(T0), sqrt_at(T0), reserve=1,
        note="NAV' = 0: burn and burnR saturate; ok = false")
    for tk, cg, res, tag in ((T0 + 40, 0, 10 ** 30, "markout-uncapped"), (T0 + 40, 0, 10 ** 15, "markout-capped"),
                             (T0 - 200, 0, 10 ** 30, "markout-other-side"), (T0 + 40, 10 ** 30, 10 ** 30, "markout-below-charge0")):
        adds(tag, g0, g1, tk, cg, res, g1, S, 10 ** 20)
    adds("markout-burn-capped-by-reserve-shares", g0, g1, T0 + 40, 0, 10 ** 30, g1, S, 1)
    adds("markout-nav-now-zero", g0, g1, T0 + 40, 0, 10 ** 30, R.Holdings(), S, 7, note="NAV_now = 0: burn every reserve share")
    adds("markout-zero-extra-nav-zero", g0, g1, T0 + 40, 10 ** 30, 10 ** 30, R.Holdings(), S, 7, note="extra 0: burn nothing even at NAV_now = 0")
    adds("markout-reserve-one-wei", g0, g1, T0 + 40, 0, 1, g1, S, 10 ** 20, note="extra capped at 1 wei: one share-rounded burn")
    sold = R.Holdings(g0.idle0 - 10 ** 9, g0.idle1 + 3 * 10 ** 15)
    adds("markout-upper-leg", g0, sold, T0 + 40, 0, 10 ** 30, sold, S, 10 ** 20, note="the vault sold token0: the upper leg is worse")
    adds("markout-tie-lower-leg", g0, g0.copy(), T0 + 40, 0, 10 ** 30, g0, S, 10 ** 20, note="equal legs: pMk = the lower leg")
    # v2.34 (s09 D9-01, D9-02): the round-up upper leg and the clamps at the tick bounds
    adds("markout-clamp-max-tick", g0, sold, R.MAX_TICK, 0, 10 ** 30, sold, S, 10 ** 20, note="tickMk at MAX_TICK: both legs clamped")
    adds("markout-clamp-near-max", g0, sold, R.MAX_TICK - 5, 0, 10 ** 30, sold, S, 10 ** 20, note="upper leg tickMk + 6 clamped to MAX_TICK")
    adds("markout-clamp-min-tick", g0, sold, R.MIN_TICK, 0, 10 ** 30, sold, S, 10 ** 20, note="tickMk at MIN_TICK: the lower leg clamped")
    adds("markout-upper-leg-rounded-up", g0, sold, T0 - 6, 0, 10 ** 30, sold, S, 10 ** 20, note="the upper leg sits at tickMk + FEE_TICKS + 1")
    add("charge-zero-nav-zero", R.Holdings(), R.Holdings(), sqrt_at(T0), sqrt_at(T0), note="no loss and no NAV: burn 0, ok")
    # ExecLedger.value
    for s in [sqrt_at(T0), sqrt_at(T0 + 500), sqrt_at(T0 + 2500), sqrt_at(T0 + 3000)]:
        addv("value", h0, s)
    addv("value-q-high-branch", R.Holdings(5, 7), (1 << 128) + 12345)
    addv("value-empty", R.Holdings(), sqrt_at(T0))
    return cases


LEDGER_CONV = {
    "measure": "loss_p = NAV_before(p) - NAV_after(p) for p in (sqrtSpot, sqrtS0, sqrtRef), a later leg replacing only on a "
               "strictly larger loss; charge = max(0, max loss); pStar = argmax (sqrtSpot on a tie, incl. charge 0); "
               "navAfterPStar = NAV_after(pStar); burn = ceilDiv(charge*S, navAfterPStar) (0 when charge = 0; "
               "type(uint256).max when navAfterPStar = 0 and charge > 0); burnR = ceilDiv((charge + reserve)*S, "
               "navAfterPStar) - burn (type(uint256).max when saturated); ok = burn + burnR <= unvested + bond, evaluated without "
               "overflow (both may be type(uint256).max).",
    "settleExtra": "legs sqrtAt(max(MIN_TICK, tickMk - feeTicks)) then sqrtAt(min(MAX_TICK, tickMk + feeTicks + 1)) (the upper "
                   "leg rounded up; the later replaces only if strictly larger); no revert on any input; "
                   "extra = min(reserve, max(0, loss_mk - charge0)); burnS = 0 if extra = 0, reserveShares if "
                   "NAV(now, pMk) = 0, else min(reserveShares, ceilDiv(extra*S, NAV(now, pMk))).",
    "NAV": "NAV(s) = q(idle0 + x_pos(s), s) + idle1 + y_pos(s); positions per Appendix A with roundUp = false.",
    "value": "ExecLedger.value(h, sqrtP) = (NAV(sqrtP), val(x_pos, y_pos, sqrtP)).",
    "holdings": "Holdings {idle0, idle1, L, lo, hi}; before/after are inputs.",
    "fields": "ExecLedger.measure in {before, after, sqrtS0, sqrtSpot, sqrtRef, reserve, S, unvested, bond} out {charge, "
              "pStar, navAfterPStar, burn, burnR, ok}; ExecLedger.settleExtra in {before, after, tickMk, feeTicks, charge0, "
              "reserve, now, S, reserveShares} out {extra, pMk, burnS}; ExecLedger.value in {h, sqrtP} out {nav, posValue}; Holdings = {idle0, idle1, "
              "L, lo, hi}.",
}


# ======================================================================== placement
def placement_cases():
    cases = []
    B, W, SP = 60, 2000, 10

    def chk(tag, lo, hi, spot, ref, expect):
        assert R.placement_check(lo, hi, spot, ref, B, W, SP) == expect, (tag, "errCode")
        cases.append(dict(id=f"placement-{len(cases):03d}-{tag}", fn="Placement.check",
                          **{"in": dict(lo=lo, hi=hi, tickSpot=spot, refTick=ref, band=B, wTicks=W, spacing=SP),
                             "out": dict(errCode=R.placement_check(lo, hi, spot, ref, B, W, SP))}))

    def size(tag, idle, lo, hi, s_spot, s_ref, below):
        L, after, d = R.placement_size(idle, lo, hi, s_spot, s_ref)
        assert (d < R.TEST.DELTA_MIN) == below, (tag, "deltaAfter", d)
        cases.append(dict(id=f"placement-{len(cases):03d}-{tag}", fn="Placement.size",
                          **{"in": dict(idle=idle.d(), lo=lo, hi=hi, sqrtSpot=s_spot, sqrtRef=s_ref),
                             "out": dict(L=L, after=after.d(), deltaAfter=d)}))

    chk("ok-centred", T0 - 1000, T0 + 1000, T0, T0, 0)
    chk("ok-edge", T0, T0 + 2000, T0 + 3, T0 + 55, 0)
    chk("fence", T0 - 1000, T0 + 1000, T0 + 61, T0, 1)
    chk("fence-exact", T0 - 1000, T0 + 1000, T0 + 60, T0, 0)
    chk("width", T0 - 1000, T0 + 1010, T0, T0, 2)
    chk("alignment", T0 - 995, T0 + 1005, T0, T0, 3)
    chk("alignment-negative", -1005, 995, -2, 0, 3)
    chk("spot-not-contained", T0 + 10, T0 + 2010, T0 + 5, T0 + 20, 4)
    chk("spot-at-hi", T0 - 2000, T0, T0, T0 - 30, 4)
    chk("ref-not-contained", T0, T0 + 2000, T0 + 5, T0 - 20, 5)
    chk("multi-fail-first", T0 - 995, T0 + 1000, T0 + 200, T0, 1)
    # boundaries, both sides (fix review r2, R2-02)
    chk("fence-below", T0 - 1000, T0 + 1000, T0 - 61, T0, 1)
    chk("fence-below-exact", T0 - 1000, T0 + 1000, T0 - 60, T0, 0)
    chk("ref-at-hi", T0 - 1960, T0 + 40, T0 + 39, T0 + 40, 5)
    chk("ref-at-lo", T0, T0 + 2000, T0 + 30, T0, 0)
    chk("spot-at-lo", T0, T0 + 2000, T0, T0 + 30, 0)
    chk("spot-below-lo", T0, T0 + 2000, T0 - 1, T0 + 30, 4)
    chk("ref-below-lo", T0, T0 + 2000, T0 + 30, T0 - 1, 5)
    chk("width-wider", T0 - 1000, T0 + 1010, T0, T0, 2)
    chk("width-narrower", T0 - 1000, T0 + 990, T0, T0, 2)
    idle = R.Holdings(1_500_000 * USDC, 500 * ETH)
    size("centred", idle, T0 - 1000, T0 + 1000, sqrt_at(T0), sqrt_at(T0), False)
    size("token0-side-edge", R.Holdings(3_000_000 * USDC, 2 * ETH), T0, T0 + 2000, sqrt_at(T0) + 1, sqrt_at(T0 + 20), False)
    size("token1-side-edge", R.Holdings(10 * USDC, 1000 * ETH), T0 - 1990, T0 + 10, sqrt_at(T0 + 10) - 1, sqrt_at(T0 - 20), False)
    size("below-range", R.Holdings(10 ** 12, 10 ** 20), T0 + 10, T0 + 2010, sqrt_at(T0), sqrt_at(T0), False)
    size("above-range", R.Holdings(10 ** 12, 10 ** 20), T0 - 2010, T0 - 10, sqrt_at(T0), sqrt_at(T0), True)
    size("edge-balanced", R.Holdings(1_500_000 * USDC, 500 * ETH), T0, T0 + 2000, sqrt_at(T0) + 1, sqrt_at(T0 + 30), False)
    size("dust-deltamin", R.Holdings(1_000 * USDC, 500 * ETH), T0, T0 + 2000, sqrt_at(T0) + 1, sqrt_at(T0 + 30), True)
    size("token1-edge-saturated-leg", R.Holdings(500_000 * USDC, 500 * ETH), T0 - 1990, T0 + 10, sqrt_at(T0 + 10) - 1,
         sqrt_at(T0), False)
    size("spot-exactly-at-lo", R.Holdings(10 ** 12, 10 ** 20), T0, T0 + 2000, sqrt_at(T0), sqrt_at(T0 + 30), False)
    size("spot-exactly-at-hi", R.Holdings(10 ** 6, 10 ** 21), T0 - 1990, T0 + 10, sqrt_at(T0 + 10), sqrt_at(T0), False)
    # exact DELTA_MIN boundary (found by bisection at a large NAV): deltaAfter == 5e17 passes, one raw unit less fails
    bx, by = 1496491847102831049, 5 * 10 ** 26
    size("deltamin-exact", R.Holdings(bx, by), T0, T0 + 2000, sqrt_at(T0) + 1, sqrt_at(T0 + 30), False)
    size("deltamin-one-below", R.Holdings(bx - 1, by), T0, T0 + 2000, sqrt_at(T0) + 1, sqrt_at(T0 + 30), True)
    size("empty-idle", R.Holdings(), T0 - 1000, T0 + 1000, sqrt_at(T0), sqrt_at(T0), True)
    size("tiny-headroom", R.Holdings(1000, 1000), T0 - 1000, T0 + 1000, sqrt_at(T0), sqrt_at(T0), True)
    size("ref-moves-delta", idle, T0 - 1000, T0 + 1000, sqrt_at(T0), sqrt_at(T0 + 60), False)
    # v2.34 (s09 D9-11): at an extreme price with spot one unit inside the upper edge, the token0 leg's quotient exceeds 2^256
    hx = R.MAX_TICK - R.MAX_TICK % SP
    size("extreme-price-leg-overflow", R.Holdings(10 ** 30, 10 ** 30), hx - W, hx, sqrt_at(hx) - 1, sqrt_at(hx - 30), True)
    return cases


PLACEMENT_CONV = {
    "check": "errCode = first failing code in numeric order: 1 |tickSpot - refTick| > band; 2 hi - lo != wTicks; "
             "3 lo or hi not a multiple of spacing; 4 not lo <= tickSpot < hi; 5 not lo <= refTick < hi; 0 ok. "
             "The vault maps 1 Fence, 2 Width, 3 Alignment, 4 and 5 NotContained.",
    "size": "L = getLiquidityForAmounts(sqrtSpot, sqrtAt(lo), sqrtAt(hi), idle0, idle1) - (L/1e9 + 1), floored at 0; "
            "after = idle minus the v4 add amounts of L at sqrtSpot (roundUp = true); "
            "deltaAfter = min(WAD, positionValue(sqrtRef)*WAD / NAV(sqrtRef)) of `after` (0 when NAV = 0). "
            "deltaAfter < DELTA_MIN (0.5 WAD) means the vault reverts DeltaMin. Legs saturate at type(uint128).max "
            "(spot one unit inside an edge, case token1-edge-saturated-leg), including a quotient above 2^256 inside the leg's "
            "mulDiv (case extreme-price-leg-overflow).",
    "fields": "Placement.check in {lo, hi, tickSpot, refTick, band, wTicks, spacing} out {errCode}; Placement.size in "
              "{idle, lo, hi, sqrtSpot, sqrtRef} out {L, after, deltaAfter}; idle/after are Holdings {idle0, idle1, L, lo, hi}.",
}


# ======================================================================== bank
def bank_cases():
    cases = []

    def st(**kw):
        b = R.Bank()
        b.dripBlocks, b.releaseDelay = 120, 480
        for k, v in kw.items():
            if k == "cohorts":
                for e, s in v:
                    b.cohorts[e % R.RING] = [e, s]
            else:
                setattr(b, k, v)
        return b

    def add(tag, b, op, args, note=""):
        if op == "Charge":
            args = {**args, "burnR": args.get("burnR", 0)}
        bin_ = b.d()
        try:
            r = R.fee_bank_apply(b, op, args)
            out = dict(bank=b.d(), burnShares=r[0], payShares=r[1], a=r[2], b2=r[3])
        except R.Revert as ex:
            out = dict(revert=ex.err)
        cases.append(dict(id=f"bank-{len(cases):03d}-{tag}", fn="FeeBank.apply",
                          **{"in": dict(bank=bin_, op=R.OPS.index(op), args=args), "out": out}))
        return b

    k = 10 ** 18
    # Accrue
    add("accrue-empty", st(bondShares=5 * k), "Accrue", dict(epoch=7, shares=3 * k, gateOpen=False))
    add("accrue-same-epoch", st(cohorts=[(7, 3 * k)]), "Accrue", dict(epoch=7, shares=2 * k, gateOpen=False))
    add("accrue-overwrite-gate-open", st(cohorts=[(2, 4 * k)]), "Accrue", dict(epoch=7, shares=k, gateOpen=True),
        note="occupant epoch 2 in slot 7 mod 5 vests")
    add("accrue-overwrite-gate-closed", st(cohorts=[(2, 4 * k)], maturedPending=k), "Accrue",
        dict(epoch=7, shares=k, gateOpen=False), note="occupant moves to maturedPending")
    add("accrue-overwrite-gate-open-adds-to-vested", st(cohorts=[(2, 4 * k)], vestedUnclaimed=3 * k), "Accrue",
        dict(epoch=7, shares=k, gateOpen=True), note="vestedUnclaimed grows by the occupant")
    add("accrue-zero-noop", st(cohorts=[(2, 4 * k)]), "Accrue", dict(epoch=7, shares=0, gateOpen=False))
    # VestTime
    full = dict(cohorts=[(3, k), (4, 2 * k), (5, 3 * k), (6, 4 * k), (7, 5 * k)], maturedPending=6 * k)
    add("vest-open", st(**full), "VestTime", dict(nowEpoch=9, gateOpen=True),
        note="epochs 3, 4 matured (e+1+V <= 9) plus maturedPending vest")
    add("vest-closed", st(**full), "VestTime", dict(nowEpoch=9, gateOpen=False), note="matured -> maturedPending")
    add("vest-nothing-matured", st(**full), "VestTime", dict(nowEpoch=7, gateOpen=True),
        note="only maturedPending vests")
    add("vest-boundary", st(cohorts=[(3, k)]), "VestTime", dict(nowEpoch=8, gateOpen=True), note="vestBlock = 8·EPOCH")
    add("vest-boundary-minus-1", st(cohorts=[(3, k)]), "VestTime", dict(nowEpoch=7, gateOpen=True))
    # Charge
    add("charge-youngest-first", st(**full, bondShares=10 * k), "Charge", dict(burn=10 * k),
        note="7 (5k), 6 (4k), 5 (1k)")
    add("charge-into-pending-and-bond", st(cohorts=[(7, k)], maturedPending=2 * k, bondShares=10 * k), "Charge",
        dict(burn=5 * k), note="cohort, then maturedPending, then bond")
    add("charge-one-into-second-cohort", st(cohorts=[(7, 3 * k), (6, 5 * k)], bondShares=k), "Charge", dict(burn=3 * k + 1),
        note="the youngest cohort, then exactly 1 share from the next cohort")
    add("charge-exact-total", st(cohorts=[(7, k)], maturedPending=k, bondShares=k), "Charge", dict(burn=3 * k))
    add("charge-insolvent", st(cohorts=[(7, k)], maturedPending=k, bondShares=k), "Charge", dict(burn=3 * k + 1))
    add("charge-zero", st(cohorts=[(7, k)], bondShares=k), "Charge", dict(burn=0))
    # repair C: the reserve (Charge with burnR, Settle; breach and retire leave it in place)
    add("charge-with-reserve", st(cohorts=[(7, k)], maturedPending=k, bondShares=10 * k), "Charge", dict(burn=k // 2, burnR=2 * k),
        note="burn youngest first, then the reserve in the same order; reserveBond = the part from the bond")
    add("charge-reserve-insolvent", st(cohorts=[(7, k)], bondShares=k), "Charge", dict(burn=k, burnR=k + 1),
        note="burn + burnR above the cover reverts Insolvent")
    rs = dict(cohorts=[(7, k)], bondShares=10 * k, reserveShares=3 * k, reserveBond=k)
    add("settle-burn-nonbond-first", st(**rs), "Settle", dict(burnS=2 * k + 5, epoch=8, now=1000),
        note="the non-bond part burns first; the bond part's remainder returns to the bond")
    add("settle-release-to-cohort", st(**rs), "Settle", dict(burnS=k, epoch=8, now=1000), note="the rest joins epoch 8's cohort")
    add("settle-release-overwrites-old-slot", st(cohorts=[(3, 4 * k)], bondShares=k, reserveShares=2 * k), "Settle",
        dict(burnS=0, epoch=8, now=1000), note="slot 8 mod 5 holds epoch 3: its occupant moves to maturedPending")
    add("settle-burn-capped", st(**rs), "Settle", dict(burnS=10 * k, epoch=8, now=1000), note="burnS above the reserve burns all of it")
    add("settle-after-breach-to-drip", st(**rs, reserveForfeit=True, forfeitPool=k, forfeitTotal=2 * k, forfeitStart=940,
                                           dripBlocks=120), "Settle", dict(burnS=k, epoch=8, now=1000),
        note="reserveForfeit: drip owed burns first, then the released part restarts the drip")
    add("breach-keeps-reserve", st(**rs), "Breach", dict(count=1, now=1000), note="the reserve stays; reserveForfeit set")
    add("retire-keeps-reserve", st(**rs), "Retire", dict(now=1000), note="the reserve stays; reserveForfeit set")
    add("charge-reserve-while-pending", st(cohorts=[(7, k)], bondShares=10 * k, reserveShares=k), "Charge", dict(burn=k, burnR=k),
        note="a second reserve while one is pending reverts MarkoutPending")
    add("charge-no-reserve-while-pending", st(cohorts=[(7, k)], bondShares=10 * k, reserveShares=k), "Charge", dict(burn=k, burnR=0),
        note="a charge without a reserve leaves the pending reserve alone")
    add("settle-release-into-same-epoch", st(cohorts=[(8, k)], maturedPending=2 * k, bondShares=k, reserveShares=3 * k), "Settle",
        dict(burnS=0, epoch=8, now=1000), note="the released part adds to epoch 8's cohort")
    add("settle-release-overwrite-adds-to-pending", st(cohorts=[(3, 4 * k)], maturedPending=2 * k, bondShares=k, reserveShares=k),
        "Settle", dict(burnS=0, epoch=8, now=1000), note="the matured occupant adds to maturedPending")
    add("settle-release-one-share-occupant", st(cohorts=[(3, 1)], bondShares=k, reserveShares=k), "Settle",
        dict(burnS=0, epoch=8, now=1000), note="an occupant of exactly one share moves to maturedPending")
    add("settle-after-breach-drip-owed", st(**rs, reserveForfeit=True, forfeitPool=k, forfeitTotal=2 * k, forfeitStart=900,
                                             dripBlocks=120), "Settle", dict(burnS=k, epoch=8, now=1000),
        note="the drip owed at now burns first and is returned in burnShares")
    add("release-excludes-reserve", st(bondShares=5 * k, reserveShares=2 * k, reserveBond=2 * k, mode=R.EXITED, exitBlock=0,
                                       releaseDelay=0), "Release", dict(shares=10 * k, now=10 ** 6),
        note="releaseBond releases bondShares only")
    # Drip
    pool = dict(forfeitPool=12 * k, forfeitTotal=12 * k, forfeitStart=1000)
    for now in (1000, 1030, 1060, 1119, 1120, 5000):
        add(f"drip-{now}", st(**pool), "Drip", dict(now=now))
    add("drip-partly-burned", st(forfeitPool=9 * k, forfeitTotal=12 * k, forfeitStart=1000), "Drip", dict(now=1060),
        note="target 6k burned so far; 3k already burned; burn 3k")
    add("drip-last-share", st(forfeitPool=1, forfeitTotal=12 * k, forfeitStart=1000), "Drip", dict(now=1130))
    add("drip-one-share", st(forfeitPool=1, forfeitTotal=1, forfeitStart=1000), "Drip", dict(now=1130))
    add("breach-one-share", st(cohorts=[(8, 1)]), "Breach", dict(count=1, now=1100))
    add("accrue-overwrite-one-share", st(cohorts=[(2, 1)]), "Accrue", dict(epoch=7, shares=5, gateOpen=True))
    add("vest-one-share", st(cohorts=[(3, 1)]), "VestTime", dict(nowEpoch=9, gateOpen=True))
    add("drip-zero-length", st(forfeitPool=5 * k, forfeitTotal=5 * k, forfeitStart=1000, dripBlocks=0), "Drip",
        dict(now=1000), note="dripBlocks 0 burns all, never divides by zero")
    add("drip-before-start", st(**pool), "Drip", dict(now=900))
    # Breach
    add("breach-normal-1", st(**full, bondShares=k), "Breach", dict(count=1, now=1100))
    add("breach-breached-2-to-3", st(**full, bondShares=k, breaches=2, mode=R.BREACHED, placedByCurator=True), "Breach",
        dict(count=1, now=1100), note="removal: exitBlock = now, placedByCurator = false")
    add("breach-two-at-once", st(**full, breaches=1, mode=R.BREACHED, placedByCurator=True), "Breach",
        dict(count=2, now=1100), note="1 + 2 >= 3 -> EXITED")
    add("breach-exited-keeps-exitblock", st(mode=R.EXITED, breaches=3, exitBlock=700, bondShares=k), "Breach",
        dict(count=1, now=1100), note="EXITED + Breach(1) -> (EXITED, 4), exitBlock unchanged")
    add("breach-empty-no-restart", st(forfeitPool=2 * k, forfeitTotal=4 * k, forfeitStart=1000), "Breach",
        dict(count=1, now=1060), note="owed drip burned; empty forfeit does not restart the drip")
    add("breach-restart", st(cohorts=[(8, 2 * k)], forfeitPool=12 * k, forfeitTotal=12 * k, forfeitStart=1000),
        "Breach", dict(count=1, now=1060), note="burn owed 6k first, then T = 6k remaining + 2k new, start = now")
    # Place / Retire
    add("place-normal", st(), "Place", {})
    add("place-breached", st(mode=R.BREACHED, breaches=2), "Place", {}, note="new run: mode NORMAL, breaches kept")
    add("place-exited", st(mode=R.EXITED, breaches=3), "Place", {}, note="BadMode: no placement after removal")
    add("retire", st(**full, placedByCurator=True, bondShares=k), "Retire", dict(now=1200))
    add("retire-empty", st(placedByCurator=True, forfeitPool=k, forfeitTotal=2 * k, forfeitStart=1100), "Retire",
        dict(now=1130), note="empty forfeit does not restart the drip")
    add("retire-exited", st(mode=R.EXITED), "Retire", dict(now=1200), note="BadMode")
    add("retire-running-drip", st(cohorts=[(8, 2 * k)], forfeitPool=12 * k, forfeitTotal=12 * k, forfeitStart=1000),
        "Retire", dict(now=1060), note="burns the 6k owed first, then T = 6k + 2k")
    # Claim / TopUp / Release
    add("claim", st(vestedUnclaimed=7 * k), "Claim", {})
    add("claim-exited", st(vestedUnclaimed=7 * k, mode=R.EXITED), "Claim", {}, note="claim open in every mode")
    add("topup", st(bondShares=k), "TopUp", dict(shares=2 * k))
    ex = dict(mode=R.EXITED, breaches=3, exitBlock=1000, bondShares=9 * k)
    add("release-locked", st(**ex), "Release", dict(shares=9 * k, now=1479))
    add("release-at-delay", st(**ex), "Release", dict(shares=9 * k, now=1480))
    add("release-partial", st(**ex), "Release", dict(shares=4 * k, now=1480))
    add("release-above-bond", st(**ex), "Release", dict(shares=20 * k, now=1480), note="pays min(shares, bondShares)")
    add("release-normal", st(bondShares=9 * k), "Release", dict(shares=9 * k, now=10 ** 6), note="BadMode")
    dm = dict(mode=R.EXITED, breaches=3, exitBlock=10000, bondShares=9 * k, dripBlocks=3600, releaseDelay=14400)
    add("release-demo-minus-1", st(**dm), "Release", dict(shares=9 * k, now=10000 + 14400 - 1),
        note="DEMO: releaseDelay = V·EPOCH = 14400 != dripBlocks 3600")
    add("release-demo-at", st(**dm), "Release", dict(shares=9 * k, now=10000 + 14400))
    b = st(mode=R.EXITED, breaches=3, exitBlock=1000, bondShares=9 * k)
    R.fee_bank_apply(b, "Breach", dict(count=1, now=1400))
    add("release-after-post-exit-breach", b, "Release", dict(shares=9 * k, now=1480),
        note="a post-exit breach does not defer the release")
    # saturation at uint128 (never revert)
    U = R.U128
    add("sat-accrue-slot", st(cohorts=[(7, U - 1)]), "Accrue", dict(epoch=7, shares=5, gateOpen=False))
    add("sat-accrue-vested", st(cohorts=[(2, 4)], vestedUnclaimed=U - 1), "Accrue", dict(epoch=7, shares=1, gateOpen=True))
    add("sat-accrue-pending", st(cohorts=[(2, 4)], maturedPending=U - 1), "Accrue", dict(epoch=7, shares=1, gateOpen=False))
    add("sat-vest-closed-pending", st(cohorts=[(3, 9)], maturedPending=U - 1), "VestTime", dict(nowEpoch=9, gateOpen=False))
    add("sat-forfeit-pool", st(cohorts=[(8, 9)], forfeitPool=U - 1, forfeitTotal=U - 1, forfeitStart=1000), "Breach",
        dict(count=1, now=1000))
    # a chained sequence: accrue x6 epochs with a closed gate, then vest when it opens, then breach
    b = st(bondShares=k, placedByCurator=True)
    for e in range(6):
        add(f"seq-accrue-{e}", b, "Accrue", dict(epoch=e, shares=(e + 1) * k, gateOpen=False))
    add("seq-vest-open", b, "VestTime", dict(nowEpoch=6, gateOpen=True))
    add("seq-breach", b, "Breach", dict(count=1, now=6 * 120 + 5))
    add("seq-drip-half", b, "Drip", dict(now=6 * 120 + 65))
    return cases


BANK_CONV = {
    "fn": "FeeBank.apply(Bank storage b, Op op, bytes args) -> (burnShares, payShares, a, b2); in.bank is the state "
          "before, out.bank after; out = {revert: <error>} when apply reverts (state unchanged).",
    "Op": "enum Op {Accrue, Breach, VestTime, Drip, Charge, Place, Retire, Claim, TopUp, Release, Settle}; in.op is the "
          "ordinal; the case id names the op.",
    "fields": "in {bank, op, args}; out {bank, burnShares, payShares, a, b2} or {revert}; Bank = {cohorts[5] {epoch, "
              "shares}, maturedPending, vestedUnclaimed, bondShares, forfeitPool, forfeitTotal, forfeitStart, "
              "dripBlocks, releaseDelay, breaches, mode, placedByCurator, exitBlock, reserveShares, reserveBond, reserveForfeit}.",
    "args": "Accrue (epoch, shares, gateOpen); Breach (count, now); VestTime (nowEpoch, gateOpen); Drip (now); "
            "Charge (burn, burnR); Place (); Retire (now); Claim (); TopUp (shares); Release (shares, now); Settle (burnS, epoch, now).",
    "returns": "Settle (released to the bond, released to the cohort or the drip; burnShares = settlement burn + drip owed); Charge (bank burned, bond burned); Breach/Retire (forfeited, mode after); VestTime/Accrue (vested, moved "
               "to maturedPending); Drip (burned, 0); Claim/TopUp/Release/Place (shares moved, 0). Breach and Retire "
               "first burn the drip owed at `now` (returned as burnShares), then forfeit; an empty forfeit does not "
               "restart the drip.",
    "cohorts": "V = 4; ring of V+1 = 5 slots {epoch, shares} at index epoch mod 5; a cohort vests when "
               "nowEpoch >= epoch + 1 + V; Charge burns cohorts youngest (highest epoch) first, then maturedPending, "
               "then the bond. A slot emptied by Charge, VestTime, Breach or Retire keeps its epoch (only shares go "
               "to 0); a slot is occupied iff shares > 0.",
    "mode": "0 NORMAL, 1 BREACHED, 2 EXITED. Release requires EXITED and now >= exitBlock + releaseDelay.",
    "tier": "dripBlocks and releaseDelay are Bank fields written at seed (TEST: 120 and 480; DEMO cases: 3600, 14400).",
}


# ======================================================================== clock (HookHarness)
OBS_FIELDS = ("med9", "tickSpot", "refTick", "refCachedAt", "refFresh", "resetStreak", "gapSinceVerify", "emptyRange",
              "tauV", "tauB", "outBlocks", "breachSeq", "pendingBreaches", "lastPendingBlock", "tauVAtLastPending",
              "gridIndex", "epoch")
HOOK_EVENTS = {"RefReadFailed": ("grid",), "RefRefreshed": ("grid", "tick"), "GapCleared": ("kind",),
               "GapSet": ("block",), "BreachLatched": ("seq", "block"), "RangeRegistered": ("lo", "hi", "liveTick")}


CLOCK_NOTES, SCENARIO_NOTES = {}, {}


class _Script:
    """The MockRefPool script: every `observe` during the current event returns the TWAP tick `v`, or fails when
    `v == "fail"` (the anchored read and the live read alike)."""

    def __init__(self, v=T0):
        self.v = v

    def source(self):
        return R.RefSource(lambda end: self.v, lambda end: self.v == "fail")


def _emitted(evs):
    out = []
    for e in evs:
        name, args = e[0], e[1:]
        keys = HOOK_EVENTS.get(name) or VAULT_EVENTS[name]
        out.append({name: dict(zip(keys, args))})
    return out


def _hook_step(h, sc, op, a):
    """Run one clock event on hook `h` (atomic: a revert leaves the state unchanged). Returns the step's `out`."""
    sc.v = a["ref"]
    n0, fz0 = len(h.events), dict(h.frozen)
    keep = {k: v for k, v in h.__dict__.items() if k != "ref"}
    snap = copy.deepcopy(keep)
    b, ts = a["block"], a["ts"]
    try:
        if op == "snapshot":
            h.snapshot(b, ts, a["sqrtPrice"], a["tick"])
        elif op == "refreshRef":
            h.refresh_ref(b, ts, a["sqrtPrice"], a["tick"])
        elif op == "refresh":
            h.refresh(b, ts)
        elif op == "arm":
            h.arm_at_seed(b, ts)
        elif op == "setVaultRange":
            h.set_vault_range(a["lo"], a["hi"], a["liveTick"], b, ts)
        elif op == "ackBreaches":
            h.ack_breaches(a["n"])
        else:
            raise ValueError(op)
    except R.Revert as ex:
        h.__dict__.update(snap)
        return dict(revert=ex.err)
    o = h.obs(b, a["tick"])
    return dict(obs={f: o[f] for f in OBS_FIELDS},
                state=dict(refGrid=h.refGrid, lastSnap=h.lastSnap, armed=h.armed, vaultLo=h.vaultLo, vaultHi=h.vaultHi,
                           epochStartRef=h.epochStartRef),
                frozen=[dict(gridIndex=g, med9=v[0], tauV=v[1], tauB=v[2]) for g, v in sorted(h.frozen.items())
                        if fz0.get(g) != v],
                emitted=_emitted(h.events[n0:]))


class ClockSeq:
    """Builds a clock case: a fresh hook, an optional witness range, then events at the TEST (or DEMO) tier."""

    def __init__(self, tier=R.TEST, phase=0, tick=T0):
        self.t, self.phase = tier, phase
        self.set_price(tick)
        self.ev = []

    def ts(self, b):
        return b * self.t.BLOCKTIME + self.phase

    def set_price(self, tick=None, sqrt=None, pool_tick=None):
        """The pool's slot0. `pool_tick` overrides the tick derived from the price: a v4 swap that stops exactly on
        an initialized tick from above stores that tick minus one (the edge's other representation)."""
        self.sqrt = sqrt if sqrt is not None else sqrt_at(tick)
        self.tick = pool_tick if pool_tick is not None else get_tick_at_sqrt_price(self.sqrt)

    def add(self, op, b, ref=T0, **kw):
        self.ev.append({op: dict(block=b, ts=self.ts(b), sqrtPrice=self.sqrt, tick=self.tick, ref=ref, **kw)})
        return self

    def arm(self, b, ref=T0):
        return self.add("snapshot", b, ref).add("arm", b, ref)

    def touches(self, b0, n, every, ref=T0):
        for i in range(n):
            self.add("refreshRef", b0 + i * every, ref)
        return b0 + (n - 1) * every


def clock_cases():
    cases = []
    t = R.TEST
    G, C = t.GRID, t.CURE_BLOCKS
    LO, HI = T0 - 1000, T0 + 1000
    OUT = T0 + 1500

    def add(tag, seq, witness=None, note=""):
        sc = _Script()
        h = R.Hook(seq.t, sc.source())
        if witness:
            h.witLo, h.witHi = witness
        steps = []
        for ev in seq.ev:
            (op, a), = ev.items()
            steps.append(_hook_step(h, sc, op, a))
        case = {"id": f"clock-{len(cases):03d}-{tag}", "fn": "HookHarness.events",
                "in": dict(tier=seq.t.NAME, witness=dict(lo=witness[0], hi=witness[1]) if witness else dict(lo=0, hi=0),
                           events=seq.ev),
                "out": dict(steps=steps)}
        if note:
            CLOCK_NOTES[case["id"]] = note
        cases.append(case)
        return steps

    # 1. EMPTY (unplaced) vault: observed out-of-range time latches at CURE and reaches breachSeq 3 after three
    s = ClockSeq().arm(1000)
    s.touches(1000 + G, 3 * C // G + 1, G)
    st = add("empty-three-breaches", s, note="EMPTY counts as out of range (vaultLo = vaultHi = 0); touched every GRID")
    assert st[-1]["obs"]["breachSeq"] == 3 and st[-1]["obs"]["emptyRange"], "EMPTY case must latch three breaches"
    # 1b. EMPTY with nobody observing: one late touch credits only the freshness window and sets the gap
    s = ClockSeq().arm(1000)
    s.add("refreshRef", 1000 + 10 * C)
    st = add("empty-unobserved", s, note="an unobserved EMPTY vault is not breached; the gap is flagged")
    assert st[-1]["obs"]["outBlocks"] == 2 * G and st[-1]["obs"]["gapSinceVerify"] and st[-1]["obs"]["breachSeq"] == 0
    # 2. registration starts a run; out-of-range observed time latches; ack
    s = ClockSeq().arm(2000)
    s.add("setVaultRange", 2000, lo=LO, hi=HI, liveTick=T0)
    end = s.touches(2000 + G, 2 * C // G + 1, G, ref=OUT)
    s.add("ackBreaches", end, ref=OUT, n=2)
    st = add("registered-two-latches-ack", s)
    assert st[-2]["obs"]["breachSeq"] == 2 and st[-1]["obs"]["pendingBreaches"] == 0
    # 3. stale boundary: now == refCachedAt + 2·GRID credits fully with no gap; +1 sets the gap (in range: τ_v)
    for extra, tag in ((0, "stale-boundary-exact-in"), (1, "stale-boundary-plus1-in")):
        s = ClockSeq().arm(3000)
        s.add("setVaultRange", 3000, lo=LO, hi=HI, liveTick=T0)
        s.add("snapshot", 3000 + 2 * G + extra)
        st = add(tag, s)
        assert st[-1]["obs"]["tauV"] == 2 * G and st[-1]["obs"]["gapSinceVerify"] == bool(extra)
    for extra, tag in ((0, "stale-boundary-exact-out"), (1, "stale-boundary-plus1-out")):
        s = ClockSeq().arm(3000)
        s.add("setVaultRange", 3000, lo=LO, hi=HI, liveTick=T0)
        s.add("refreshRef", 3000 + G, ref=OUT)
        s.add("snapshot", 3000 + 3 * G + extra, ref=OUT)
        st = add(tag, s)
        assert st[-1]["obs"]["outBlocks"] == 2 * G and st[-1]["obs"]["gapSinceVerify"] == bool(extra)
    # 4. gap -> RESET_RUN consecutive in-range anchors -> gap cleared (kind 1), outBlocks 0
    s = ClockSeq().arm(4000)
    s.add("setVaultRange", 4000, lo=LO, hi=HI, liveTick=T0)
    s.touches(4000 + G, 3, G, ref=OUT)
    s.add("refreshRef", 4000 + 12 * G, ref=T0)   # gap: closes with the old (out) cache first, then reads in range
    s.add("refreshRef", 4000 + 13 * G, ref=T0)   # second consecutive in-range anchor: reset
    st = add("gap-then-reset", s, note="refreshRef after a gap closes the span with the old cache first")
    assert st[-2]["obs"]["gapSinceVerify"] and not st[-1]["obs"]["gapSinceVerify"] and st[-1]["obs"]["outBlocks"] == 0
    # 5. non-consecutive in-range anchors do not reset (streak restarts at 1)
    s = ClockSeq().arm(5000)
    s.add("setVaultRange", 5000, lo=LO, hi=HI, liveTick=T0)
    s.touches(5000 + G, 4, G, ref=OUT)
    s.add("refreshRef", 5000 + 5 * G, ref=T0)
    s.add("refreshRef", 5000 + 7 * G, ref=T0)    # anchor skipped: streak 1 again
    s.add("refreshRef", 5000 + 8 * G, ref=OUT)   # out: streak 0
    st = add("reset-streak-nonconsecutive", s)
    assert st[-1]["obs"]["outBlocks"] > 0 and st[-1]["obs"]["resetStreak"] == 0
    # 6. failed read changes nothing; a retry in a later block of the same anchor commits
    s = ClockSeq().arm(6000)
    s.add("setVaultRange", 6000, lo=LO, hi=HI, liveTick=T0)
    s.add("refreshRef", 6000 + G, ref="fail")
    s.add("refreshRef", 6000 + G + 3, ref=T0 + 7)
    s.add("refreshRef", 6000 + G + 5, ref=T0 + 9)   # same anchor: no second read
    st = add("failed-read-then-retry", s)
    assert st[-3]["emitted"][-1] == {"RefReadFailed": {"grid": (6000 + G) * 12 // 240}} and st[-1]["obs"]["refTick"] == T0 + 7
    # 7. arm with an unreadable reference reverts
    s = ClockSeq().add("snapshot", 7000).add("arm", 7000, ref="fail")
    st = add("arm-unreadable-reverts", s)
    assert st[-1] == {"revert": "RefUnreadable"}
    # 8. parking: sqrtPrice exactly at either edge earns no τ_v; one unit inside earns
    for tag, sq in (("park-lo-exact", sqrt_at(LO)), ("park-lo-plus1", sqrt_at(LO) + 1),
                    ("park-hi-exact", sqrt_at(HI)), ("park-hi-minus1", sqrt_at(HI) - 1)):
        s = ClockSeq().arm(8000)
        s.add("setVaultRange", 8000, lo=LO, hi=HI, liveTick=T0)
        s.set_price(sqrt=sq)
        s.add("snapshot", 8001)
        s.touches(8000 + G, 3, G)
        st = add(tag, s, note="τ_v needs the pool sqrtPrice strictly inside (sqrtAt(lo), sqrtAt(hi))")
        assert (st[-1]["obs"]["tauV"] > 0) == tag.endswith(("plus1", "minus1"))
    # 8b. the upper edge reached from above: v4 stores tick hi - 1 with sqrtPrice = sqrtAt(hi); no τ_v (s06 R1-01)
    s = ClockSeq().arm(8000)
    s.add("setVaultRange", 8000, lo=LO, hi=HI, liveTick=T0)
    s.set_price(sqrt=sqrt_at(HI), pool_tick=HI - 1)
    s.add("snapshot", 8001)
    s.touches(8000 + G, 3, G)
    st = add("park-hi-exact-downcross", s, note="slot0 (sqrtAt(hi), hi - 1): the price is on the edge, not inside")
    assert st[-1]["obs"]["tauV"] == 0 and st[-1]["obs"]["tickSpot"] == HI - 1
    # 9. dodge: the pool price pushed inside does not stop the clock while the reference is out
    s = ClockSeq().arm(9000)
    s.add("setVaultRange", 9000, lo=LO, hi=HI, liveTick=T0)
    s.touches(9000 + G, C // G + 1, G, ref=OUT)
    st = add("dodge-pool-push", s)
    assert st[-1]["obs"]["breachSeq"] == 1 and st[-1]["obs"]["tauV"] == G  # only the span before the first out-of-range sample
    # 10. grief: the pool held far outside does not run the clock while the reference is in range
    s = ClockSeq().arm(10000)
    s.add("setVaultRange", 10000, lo=LO, hi=HI, liveTick=T0)
    s.set_price(OUT + 500)
    s.touches(10000 + G, C // G + 2, G)
    st = add("grief-pool-held-off", s)
    assert st[-1]["obs"]["outBlocks"] == 0 and st[-1]["obs"]["tauV"] == 0
    # 11. unarmed snapshots record ring entries only; med9 over fewer than 9 entries is the lower median
    s = ClockSeq()
    for i, tk in enumerate((T0, T0 + 30, T0 - 20, T0 + 10)):
        s.set_price(tk)
        s.add("snapshot", 11000 + i)
    st = add("unarmed-ring-only", s)
    assert st[-1]["obs"]["med9"] == T0 and st[-1]["obs"]["tauV"] == 0 and not st[-1]["state"]["armed"]
    # 12. ring wrap and med9 over 40 consecutive blocks
    s = ClockSeq().arm(12000)
    rng = random.Random(12)
    for i in range(1, 41):
        s.set_price(T0 + rng.randint(-80, 80))
        s.add("snapshot", 12000 + i)
    add("ring-wrap-med9", s)
    # 13. witness τ_b
    s = ClockSeq().arm(13000)
    s.add("setVaultRange", 13000, lo=LO, hi=HI, liveTick=T0)
    s.touches(13000 + G, 4, G)
    s.set_price(T0 + 1200)
    s.touches(13000 + 5 * G, 3, G)
    st = add("witness-taub", s, witness=(T0 - 1000, T0 + 1000))
    assert st[-1]["obs"]["tauB"] == 4 * G  # the witness span ends when the pool leaves it
    # 14. a long span freezes at most the last 8 boundaries, credited only to refCachedAt + 2·GRID
    s = ClockSeq().arm(14000)
    s.add("setVaultRange", 14000, lo=LO, hi=HI, liveTick=T0)
    s.add("refreshRef", 14000 + 15 * G + 7)
    st = add("frozen-last-eight", s)
    assert len(st[-1]["frozen"]) == 8
    # 15. registration keeps pending breaches; writes the cache; clears the gap; outBlocks 0
    s = ClockSeq().arm(15000)
    s.add("setVaultRange", 15000, lo=LO, hi=HI, liveTick=T0)
    s.touches(15000 + G, C // G + 1, G, ref=OUT)
    s.add("snapshot", 15000 + (C // G + 6) * G, ref=OUT)        # gap
    s.add("setVaultRange", 15000 + (C // G + 6) * G, ref=OUT, lo=LO + 1500, hi=HI + 1500, liveTick=OUT)
    st = add("register-keeps-pending", s)
    o = st[-1]["obs"]
    assert o["pendingBreaches"] == 1 and not o["gapSinceVerify"] and o["outBlocks"] == 0 and o["refTick"] == OUT
    # 16. latch block and τ_v at the latch (in range first, then out)
    s = ClockSeq().arm(16000)
    s.add("setVaultRange", 16000, lo=LO, hi=HI, liveTick=T0)
    s.touches(16000 + G, 5, G)
    s.touches(16000 + 6 * G, C // G + 1, G, ref=OUT)
    st = add("latch-block-and-tauv", s)
    assert st[-1]["obs"]["tauVAtLastPending"] == st[-1]["obs"]["tauV"] > 0
    # 17. epochStartRef: the first cache write of a new epoch
    s = ClockSeq().arm(17000)
    s.touches(17000 + G, 8, G, ref=T0 + 40)
    add("epoch-start-ref", s)
    # 18. DEMO tier: two callers in one anchor get the same value; phases 0 / 150 / 299
    for ph in (0, 150, 299):
        s = ClockSeq(tier=R.DEMO, phase=ph).arm(36000)
        s.add("setVaultRange", 36000, lo=LO, hi=HI, liveTick=T0)
        s.add("refreshRef", 36000 + 310, ref=T0 + 11)
        s.add("refreshRef", 36000 + 320, ref=T0 + 99)
        s.add("refreshRef", 36000 + 610, ref=T0 + 13)
        st = add(f"demo-anchor-phase{ph}", s, note="anchor k = floor(ts / GRID_SEC); one read per anchor")
    # 19. TEST with a phase of 150 s: one anchor spanning two block grids refreshes once
    s = ClockSeq(phase=150).arm(19000)
    s.add("setVaultRange", 19000, lo=LO, hi=HI, liveTick=T0)
    for i, b in enumerate((19008, 19012, 19020, 19028, 19033)):
        s.add("refreshRef", b, ref=T0 + i)
    add("test-anchor-phase150", s)
    # 20. the internal _refresh alone (steps 1 and 5) after a snapshot in the same block
    s = ClockSeq().arm(20000)
    s.add("setVaultRange", 20000, lo=LO, hi=HI, liveTick=T0)
    s.add("snapshot", 20000 + G, ref="fail")
    s.add("refresh", 20000 + G, ref=T0 + 5)
    add("refresh-after-failed-snapshot-read", s)
    # 21-50. random sequences (snapshot, refreshRef, registration, ack, failure, parking, phases)
    rng = random.Random(2002)
    for i in range(30):
        tier = R.TEST if i % 3 else R.DEMO
        s = ClockSeq(tier=tier, phase=rng.choice((0, 7, 150))).arm(30000 if tier is R.TEST else 360000)
        b = s.ev[-1]["arm"]["block"]
        lo, hi = 0, 0
        for _ in range(rng.randint(15, 30)):
            b += rng.choice((1, 3, tier.GRID // 2, tier.GRID, tier.GRID + 1, 2 * tier.GRID, 2 * tier.GRID + 1, 7 * tier.GRID))
            ref = rng.choice((T0, T0 + 900, T0 - 1100, OUT, "fail", T0 + rng.randint(-1200, 1200)))
            r = rng.random()
            if hi and r < 0.15:
                s.set_price(sqrt=rng.choice((sqrt_at(lo), sqrt_at(lo) + 1, sqrt_at(hi), sqrt_at(hi) - 1)))
            elif r < 0.5:
                s.set_price(T0 + rng.randint(-1300, 1300))
            op = rng.random()
            if op < 0.12 and ref != "fail":
                lt = ref
                c = lt // 10 * 10 - rng.choice((0, 500, 1990))
                lo, hi = c, c + 2000
                s.add("setVaultRange", b, ref=ref, lo=lo, hi=hi, liveTick=lt)
            elif op < 0.18:
                s.add("ackBreaches", b, ref=ref, n=rng.randint(0, 2))
            elif op < 0.55:
                s.add("snapshot", b, ref=ref)
            else:
                s.add("refreshRef", b, ref=ref)
        add(f"random-{i:02d}", s, witness=(T0 - 1000, T0 + 1000) if i % 2 else None)
    return cases


CLOCK_CONV = {
    "fn": "HookHarness.events: the hook (TouchstoneHook with an internal virtual _slot0() seam and a scripted "
          "MockRefPool) is constructed fresh per case; in.witness sets witLo/witHi by a harness setter; then each "
          "event of in.events runs in order at vm.roll(block) / vm.warp(ts) with the pool slot0 = (sqrtPrice, tick).",
    "events": "each event is {op: {block, ts, sqrtPrice, tick, ref, ...}}; op ∈ snapshot (snapshotIfNewBlock), "
              "refreshRef, refresh (the internal _refresh: steps 1 and 5 only, exposed by the harness), arm "
              "(armAtSeed; the vault's seed snapshot is a separate preceding event), setVaultRange {lo, hi, liveTick} "
              "(called as the vault), ackBreaches {n} (called as the vault). `ref` is what every MockRefPool observe "
              "returns during that event: a TWAP tick, or \"fail\" (the call reverts inside the mock).",
    "out": "out.steps[i] for event i: {obs (the Obs struct after the event, at block = the event's block and the "
           "event's pool tick), state {refGrid, lastSnap, armed, vaultLo, vaultHi, epochStartRef}, frozen (the "
           "frozen(gridIndex) entries written or changed by the event), emitted (the hook's events in order: "
           "RefReadFailed(grid), RefRefreshed(grid, tick), GapCleared(kind), GapSet(block), BreachLatched(seq, block), "
           "RangeRegistered(lo, hi, liveTick))} or {revert: <error>} with the state unchanged.",
    "tiers": "TEST: blocktime 12 s, GRID 20, GRID_SEC 240, EPOCH 120, CURE_BLOCKS 200, RESET_RUN 2; DEMO: blocktime 1 s, "
             "GRID 300, GRID_SEC 300, EPOCH 3600, CURE_BLOCKS 1800; RESET_RUN 2 in both. ts = block × blocktime + phase.",
    "conventions": "an unarmed snapshot records its ring entry only; med9 over fewer than 9 entries is the lower "
                   "median; EMPTY stores vaultLo = vaultHi = 0 and counts as out of range; armAtSeed whose refresh "
                   "fails reverts RefUnreadable; ackBreaches(n) with n > pendingBreaches saturates pendingBreaches "
                   "at 0 (never reverts).",
    "notes": CLOCK_NOTES,
}


# ======================================================================== mode (Ladder)
def mode_cases():
    cases = []

    def nxt(tag, mode, breaches, ev, count):
        try:
            m, b, x = R.ladder_next(mode, breaches, ev, count)
            out = dict(mode=m, breaches=b, exited=x)
        except R.Revert as e:
            out = dict(revert=e.err)
        cases.append({"id": f"mode-{len(cases):03d}-{tag}", "fn": "Ladder.next",
                      "in": dict(mode=mode, breaches=breaches, ev=ev, count=count), "out": out})

    U = R.U32
    for mode in (R.NORMAL, R.BREACHED, R.EXITED):
        for br in (0, 1, 2, 3, U - 1):
            for cnt in (0, 1, 2, 3, U):
                nxt(f"next-m{mode}-b{br}-breach{cnt}", mode, br, R.EV_BREACH, cnt)
        for ev, nm in ((R.EV_PLACE, "place"), (R.EV_RETIRE, "retire")):
            for br in (0, 2, 3):
                for cnt in (0, 1):
                    nxt(f"next-m{mode}-b{br}-{nm}{cnt}", mode, br, ev, cnt)
    for mode in (R.NORMAL, R.BREACHED, R.EXITED):
        for ent, name in enumerate(R.ENTRY):
            cases.append({"id": f"mode-{len(cases):03d}-allowed-m{mode}-{name}", "fn": "Ladder.allowed",
                          "in": dict(mode=mode, entry=ent), "out": dict(allowed=R.ladder_allowed(mode, ent))})
    # sequences: each event applies Ladder.next to the previous output; a revert leaves the state unchanged
    seqs = {
        "seq-three-single-breaches": [(R.EV_BREACH, 1)] * 4,
        "seq-place-does-not-reset-breaches": [(R.EV_BREACH, 1), (R.EV_PLACE, 0), (R.EV_BREACH, 1), (R.EV_PLACE, 0),
                                              (R.EV_BREACH, 1), (R.EV_PLACE, 0)],
        "seq-three-at-once": [(R.EV_BREACH, 3), (R.EV_BREACH, 1), (R.EV_RETIRE, 0)],
        "seq-retire-then-breach": [(R.EV_RETIRE, 0), (R.EV_BREACH, 1), (R.EV_PLACE, 0), (R.EV_BREACH, 0)],
        "seq-zero-count-identity": [(R.EV_BREACH, 0), (R.EV_BREACH, 2), (R.EV_BREACH, 0), (R.EV_BREACH, 1)],
    }
    for tag, evs in seqs.items():
        mode, br = R.NORMAL, 0
        steps = []
        for ev, cnt in evs:
            try:
                mode, br, x = R.ladder_next(mode, br, ev, cnt)
                steps.append(dict(mode=mode, breaches=br, exited=x))
            except R.Revert as e:
                steps.append(dict(revert=e.err))
        cases.append({"id": f"mode-{len(cases):03d}-{tag}", "fn": "Ladder.next",
                      "in": dict(mode=R.NORMAL, breaches=0, events=[dict(ev=e, count=c) for e, c in evs]),
                      "out": dict(steps=steps)})
    return cases


MODE_CONV = {
    "fn": "Ladder.next(uint8 mode, uint32 breaches, uint8 ev, uint32 count) -> (mode, breaches, exited) and "
          "Ladder.allowed(uint8 mode, uint8 entry) -> bool, both pure.",
    "encodings": "mode 0 NORMAL, 1 BREACHED, 2 EXITED; ev 0 Breach, 1 Place, 2 Retire; entry = uint8(Entry) with "
                 "Entry {Deposit, Withdraw, SetPositions, RebalanceSwap, Claim, TopUpBond, Retire, ReleaseBond, Poke, "
                 "ProcessBreach, Seed, Settle}.",
    "single": "in {mode, breaches, ev, count} -> out {mode, breaches, exited} or {revert: BadMode}. breaches saturate "
              "at type(uint32).max; Breach with count 0 is the identity; exited is true only on a transition from "
              "NORMAL or BREACHED to EXITED; Place and Retire on EXITED revert BadMode.",
    "allowed": "in {mode, entry} -> out {allowed}: the entry-point table of the mode machine; allowed(·, Seed) is false "
               "(seed is gated by onlyFactory and once).",
    "sequences": "in {mode, breaches, events[{ev, count}]}: Ladder.next applied in order to the previous output; out.steps[i] "
                 "is the result after event i ({revert} leaves the state unchanged).",
}


# ======================================================================== scenario (VaultHarness.checkpoint)
VAULT_EVENTS = {"Checkpoint": ("tauV", "tauB", "nav0", "delta", "base", "L", "lo", "hi"),
                "BreachProcessed": ("count", "lastBlock", "forfeited"), "CheckpointDeferred": (),
                "ackBreaches": ("n",)}


class _AckStub:
    """The harness stubs `ackBreaches`: it records the call (emitted as {ackBreaches: {n}})."""

    def __init__(self, vault, lo, hi):
        self.vault, self.vaultLo, self.vaultHi = vault, lo, hi

    def try_ref(self, ts):
        return self.vault._live_src.read(ts, 0)

    def ack_breaches(self, n):
        self.vault.events.append(("ackBreaches", n))


def _vault_state(v):
    recs = {}
    for e, r in sorted(v.records.items()):
        recs[str(e)] = dict(base=r["base"], minted=r["minted"], forfeited=r["forfeited"],
                            forfeitValue=r["forfeitValue"], flags=sorted(r["flags"]), end=r.get("end", 0))
    b = v.bank  # serialised field by field (not through Bank.d) so a defect there is caught by the vectors, not a crash
    bank = {"cohorts": [{"epoch": e, "shares": s_} for e, s_ in b.cohorts], **{f: getattr(b, f) for f in R.Bank.FIELDS}}
    return dict(holdings=v.h.d(), bank=bank, NAV0=v.NAV0, delta0=v.delta0, tauV0=v.tauV0, segStart=v.segStart,
                S=v.S, baseOwed=v.baseOwed, epoch=v.epoch, epochSwapFrac=v.epochSwapFrac, swapCount=v.swapCount,
                records=recs)


def _new_harness(tier, st):
    v = R.Vault(tier, None, None)
    v.h = R.Holdings(**st["holdings"])
    b = R.Bank()
    for k2, val in st["bank"].items():
        if k2 == "cohorts":
            b.cohorts = [[c["epoch"], c["shares"]] for c in val]
        else:
            setattr(b, k2, val)
    v.bank = b
    for k2 in ("NAV0", "delta0", "tauV0", "segStart", "S", "baseOwed", "epoch", "epochSwapFrac", "swapCount"):
        setattr(v, k2, st[k2])
    v.hook = _AckStub(v, st["holdings"]["lo"], st["holdings"]["hi"])
    return v


def scenario_cases():
    cases = []
    t = R.TEST
    LO, HI = T0 - 1000, T0 + 1000
    k = 10 ** 18

    def obs(now, tauV, refFresh=True, refTick=T0, gap=False, empty=False, pending=0, lastPending=0, tauVAt=0, tauB=0):
        return dict(med9=T0, tickSpot=T0, refTick=refTick, refCachedAt=now - (1 if refFresh else 2 * t.GRID + 1),
                    refFresh=refFresh, resetStreak=0, gapSinceVerify=gap, emptyRange=empty, tauV=tauV, tauB=tauB,
                    outBlocks=0, breachSeq=pending, pendingBreaches=pending, lastPendingBlock=lastPending,
                    tauVAtLastPending=tauVAt, gridIndex=now // t.GRID, epoch=now // t.EPOCH)

    def state(now, cohorts=(), mp=0, vu=0, bond=10 ** 21, mode=R.NORMAL, placed=True, breaches=0, fpool=0, ftot=0,
              fstart=0, empty=False, S=S_SEED, baseOwed=0, tauV0=0, segStart=None, epoch=None, idle=(150_000 * USDC, 50 * ETH),
              L=0, delta0=WAD // 2, exitBlock=0):
        lo, hi = (0, 0) if empty else (LO, HI)
        bank = R.Bank()
        for e, s_ in cohorts:
            bank.cohorts[e % R.RING] = [e, s_]
        bank.maturedPending, bank.vestedUnclaimed, bank.bondShares = mp, vu, bond
        bank.mode, bank.placedByCurator, bank.breaches, bank.exitBlock = mode, placed, breaches, exitBlock
        bank.forfeitPool, bank.forfeitTotal, bank.forfeitStart = fpool, ftot, fstart
        bank.dripBlocks, bank.releaseDelay = t.EPOCH, R.V * t.EPOCH
        h = R.Holdings(idle[0], idle[1], L, lo, hi)
        nav0 = R.value(h, sqrt_at(T0))[0]
        return dict(holdings=h.d(), bank=bank.d(), NAV0=nav0, delta0=0 if empty else delta0, tauV0=tauV0,
                    segStart=now - 50 if segStart is None else segStart, S=S, baseOwed=baseOwed,
                    epoch=now // t.EPOCH if epoch is None else epoch, epochSwapFrac=0, swapCount=0, records={})

    def add(tag, st, events, note=""):
        v = _new_harness(t, st)
        steps = []
        for ev in events:
            v._live_src = R.RefSource(lambda end, x=ev["live"]: 0 if x == "fail" else x, lambda end, x=ev["live"]: x == "fail")
            n0 = len(v.events)
            v._checkpoint(ev["obs"], ev["block"], ev["ts"])
            reads = getattr(v._live_src, "live_count", 0)
            assert reads <= 1, "a checkpoint reads the live reference at most once"
            steps.append(dict(state=_vault_state(v), liveReads=reads, emitted=_emitted(v.events[n0:])))
        case = {"id": f"scenario-{len(cases):03d}-{tag}", "fn": "VaultHarness.checkpoint",
                "in": dict(tier=t.NAME, state=st, events=events), "out": dict(steps=steps)}
        if note:
            SCENARIO_NOTES[case["id"]] = note
        cases.append(case)
        return steps

    def ev(now, o, live=T0, phase=0):
        return dict(block=now, ts=now * t.BLOCKTIME + phase, obs=o, live=live)

    n = 1200  # epoch 10 at TEST
    # gate legs with a matured cohort (epoch 5 vests at epoch 10)
    st = add("gate-open-vests", state(n, cohorts=[(5, 7 * k)]), [ev(n, obs(n, 40), T0)])
    assert st[0]["liveReads"] == 1 and st[0]["state"]["bank"]["vestedUnclaimed"] == 7 * k
    st = add("gate-live-out", state(n, cohorts=[(5, 7 * k)]), [ev(n, obs(n, 40), T0 + 1500)],
             note="cache fresh and in range, live reference out: no vest, matured cohort to maturedPending")
    assert st[0]["state"]["bank"]["maturedPending"] == 7 * k
    st = add("gate-live-fail", state(n, cohorts=[(5, 7 * k)]), [ev(n, obs(n, 40), "fail")])
    assert st[0]["state"]["bank"]["maturedPending"] == 7 * k and st[0]["liveReads"] == 1
    st = add("gate-live-fail-tick0-inside", dict(state(n, cohorts=[(5, 7 * k)]), holdings=dict(idle0=10 ** 11, idle1=50 * ETH, L=0, lo=-1000, hi=1000)),
             [ev(n, obs(n, 40, refTick=0), "fail")], note="a failed read whose tick field is 0 inside the range must not open the gate")
    assert st[0]["state"]["bank"]["vestedUnclaimed"] == 0
    for tag, o in (("gate-cache-stale", obs(n, 40, refFresh=False)), ("gate-cache-out", obs(n, 40, refTick=T0 + 1500)),
                   ("gate-gap", obs(n, 40, gap=True))):
        st = add(tag, state(n, cohorts=[(5, 7 * k)]), [ev(n, o, T0)])
        assert st[0]["liveReads"] == (1 if tag == "gate-cache-stale" else 0)  # a stale cache reads tryRef for the mint
    st = add("gate-empty", state(n, cohorts=[(5, 7 * k)], empty=True, placed=False), [ev(n, obs(n, 40, empty=True), T0)])
    assert st[0]["liveReads"] == 0
    # read only when vestable
    st = add("not-vestable-no-read", state(n, cohorts=[(8, 7 * k)]), [ev(n, obs(n, 40), T0)])
    assert st[0]["liveReads"] == 0
    st = add("only-maturedPending-reads", state(n, cohorts=[(8, 7 * k)], mp=3 * k), [ev(n, obs(n, 40), T0 + 1500)],
             note="only maturedPending matured: the live leg is read and an out-of-range live reference blocks the vest")
    assert st[0]["liveReads"] == 1 and st[0]["state"]["bank"]["maturedPending"] == 3 * k
    st = add("only-maturedPending-vests", state(n, cohorts=[(8, 7 * k)], mp=3 * k), [ev(n, obs(n, 40), T0)])
    assert st[0]["state"]["bank"]["vestedUnclaimed"] == 3 * k
    # pending breach with a matured cohort: one live read that step (3) makes moot
    st = add("breach-with-matured-cohort", state(n, cohorts=[(5, 7 * k), (9, 2 * k)], tauV0=10),
             [ev(n, obs(n, 60, pending=1, lastPending=n - 20, tauVAt=30), T0)])
    s0 = st[0]
    assert s0["liveReads"] == 1 and s0["state"]["bank"]["forfeitPool"] == 9 * k and s0["state"]["bank"]["vestedUnclaimed"] == 0
    # forfeiture of unminted base up to b_last and baseOwed; base after b_last accrues
    st = add("breach-forfeits-base-and-owed", state(n, cohorts=[(9, 2 * k)], baseOwed=5 * 10 ** 12, tauV0=0, segStart=n - 100),
             [ev(n, obs(n, 90, pending=2, lastPending=n - 30, tauVAt=70), T0)])
    assert st[0]["state"]["baseOwed"] == 0 and st[0]["state"]["bank"]["breaches"] == 2
    st = add("third-breach-exits", state(n, cohorts=[(9, 2 * k)], breaches=2, mode=R.BREACHED),
             [ev(n, obs(n, 60, pending=1, lastPending=n - 5, tauVAt=60), T0)])
    assert st[0]["state"]["bank"]["mode"] == R.EXITED and st[0]["state"]["bank"]["exitBlock"] == n
    st = add("exited-breach-keeps-exitblock", state(n, mode=R.EXITED, placed=False, breaches=3, exitBlock=n - 300),
             [ev(n, obs(n, 60, pending=1, lastPending=n - 5, tauVAt=60), T0)])
    assert st[0]["state"]["bank"]["exitBlock"] == n - 300 and st[0]["state"]["bank"]["breaches"] == 4
    # accrual: base minted at the fresh cache price; not placed -> no base; BREACHED accrues
    st = add("accrue-fresh-cache", state(n), [ev(n, obs(n, 50, refTick=T0 + 30), T0)])
    assert st[0]["state"]["bank"]["cohorts"][n // t.EPOCH % R.RING]["shares"] > 0
    add("accrue-not-placed", state(n, placed=False), [ev(n, obs(n, 50), T0)])
    add("accrue-breached", state(n, mode=R.BREACHED, breaches=1), [ev(n, obs(n, 50), T0)])
    add("accrue-exited-none", state(n, mode=R.EXITED, placed=False, breaches=3), [ev(n, obs(n, 50), T0)])
    # deferral: stale cache, tryRef fails -> baseOwed; then minted at the tryRef price next checkpoint
    st = add("defer-then-mint-at-tryref", state(n), [ev(n, obs(n, 50, refFresh=False), "fail"),
                                                     ev(n + 7, obs(n + 7, 57, refFresh=False), T0 - 40)])
    assert st[0]["state"]["baseOwed"] > 0 and st[1]["state"]["baseOwed"] == 0 and st[0]["liveReads"] == 1
    st = add("two-unpriced-checkpoints", state(n), [ev(n, obs(n, 50, refFresh=False), "fail"),
                                                    ev(n + 9, obs(n + 9, 59, refFresh=False), "fail")])
    assert st[1]["state"]["baseOwed"] > st[0]["state"]["baseOwed"] > 0
    st = add("navp-not-above-amount-defers", dict(state(n), holdings=dict(idle0=0, idle1=10, L=0, lo=LO, hi=HI)),
             [ev(n, obs(n, 10 ** 9), T0)])
    assert st[0]["state"]["baseOwed"] > 0
    # ring overwrite of a matured occupant: gate open -> vested; closed -> maturedPending
    st = add("overwrite-gate-open", state(n, cohorts=[(5, 4 * k)]), [ev(n, obs(n, 40), T0)])
    st = add("overwrite-gate-closed", state(n, cohorts=[(5, 4 * k)]), [ev(n, obs(n, 40), T0 + 1500)])
    assert st[0]["state"]["bank"]["maturedPending"] == 4 * k
    # drip: forfeit pool burns over dripBlocks; S decreases
    st = add("drip-half", state(n, fpool=10 * k, ftot=10 * k, fstart=n - t.EPOCH // 2), [ev(n, obs(n, 0), T0)])
    assert st[0]["state"]["bank"]["forfeitPool"] == 5 * k
    # epoch roll: one record closed; a skipped epoch flagged stale; swap counters reset
    st0 = state(n, epoch=8)
    st0["epochSwapFrac"], st0["swapCount"] = 3 * 10 ** 16, 4
    st = add("epoch-roll-skip", st0, [ev(n, obs(n, 0), T0)])
    assert "stale" in st[0]["state"]["records"]["9"]["flags"] and st[0]["state"]["swapCount"] == 0
    # unverified flag; S == 0 skip
    add("unverified-flag", state(n), [ev(n, obs(n, 40, gap=True), T0)])
    st = add("unseeded-skip", state(n, S=0), [ev(n, obs(n, 40), T0)])
    assert st[0]["emitted"] == []
    # phase on the timestamp (the checkpoint does not read ts except for the live read)
    add("phase-150", state(n, cohorts=[(5, 7 * k)]), [ev(n, obs(n, 40), T0, phase=150)])
    # a multi-event sequence through accrual, vesting and a breach
    evs = []
    tv = 0
    for i in range(1, 9):
        now = n + i * 60
        tv += 55
        evs.append(ev(now, obs(now, tv, refTick=T0 + 5 * i), T0 + 5 * i))
    evs.append(ev(n + 600, obs(n + 600, tv, pending=1, lastPending=n + 590, tauVAt=tv), T0))
    add("sequence-accrue-vest-breach", state(n, cohorts=[(6, 3 * k)]), evs)
    # random states
    rng = random.Random(3003)
    for i in range(30):
        now = rng.randrange(1200, 3600)
        ne = now // t.EPOCH
        empty = rng.random() < 0.1
        coh = [(e, rng.choice((0, rng.randint(1, 10 ** 22)))) for e in range(max(0, ne - 6), ne + 1)]
        coh = coh[-R.RING:]
        st0 = state(now, cohorts=coh, mp=rng.choice((0, 0, rng.randint(1, 10 ** 21))), vu=rng.randint(0, 10 ** 20),
                    bond=rng.randint(10 ** 19, 10 ** 23), mode=rng.choice((0, 0, 1, 2)), placed=not empty and rng.random() < 0.85,
                    breaches=rng.randint(0, 3), empty=empty, baseOwed=rng.choice((0, rng.randint(1, 10 ** 15))),
                    tauV0=rng.randint(0, 500), segStart=now - rng.randint(1, 400), epoch=ne - rng.choice((0, 0, 1, 2)),
                    L=0 if empty else rng.choice((0, rng.randint(10 ** 15, 10 ** 18))), delta0=rng.randint(0, WAD),
                    fpool=rng.choice((0, rng.randint(1, 10 ** 21))), fstart=now - rng.randint(0, 200))
        st0["bank"]["forfeitTotal"] = st0["bank"]["forfeitPool"] + rng.choice((0, rng.randint(0, 10 ** 20)))
        if st0["bank"]["mode"] == R.EXITED:
            st0["bank"]["placedByCurator"] = False
        tauV = st0["tauV0"] + rng.randint(0, 300)
        pend = rng.choice((0, 0, 0, 1, 2))
        o = obs(now, tauV, refFresh=rng.random() < 0.8, refTick=rng.choice((T0, T0 + 999, T0 - 1000, T0 + 1000, T0 + 1500)),
                gap=rng.random() < 0.2, empty=empty, pending=pend,
                lastPending=rng.randint(st0["segStart"], now) if pend else 0,
                tauVAt=rng.randint(st0["tauV0"], tauV) if pend else 0)
        live = rng.choice((T0, T0 + 1000, T0 - 1001, "fail", T0 + rng.randint(-900, 900)))
        add(f"random-{i:02d}", st0, [ev(now, o, live, phase=rng.choice((0, 150)))])
    return cases


S_SEED, WAD = R.S_SEED, R.WAD

SCENARIO_CONV = {
    "fn": "VaultHarness.checkpoint(Obs o, bool liveOk, int24 liveTick): the vault's _checkpoint(o) with no PoolManager; "
          "the harness sets the state of in.state through its setters, stubs ackBreaches (recorded as an emitted "
          "{ackBreaches: {n}}) and overrides _tryRef() to return (liveOk, liveTick) and count its calls.",
    "events": "in.events[i] = {block, ts, obs, live}: vm.roll(block), vm.warp(ts), then checkpoint(obs, live != \"fail\", "
              "live == \"fail\" ? 0 : live). The state carries over between events.",
    "state": "{holdings {idle0, idle1, L, lo, hi} (lo/hi = the registered range; EMPTY = 0/0), bank (as bank.json), NAV0, "
             "delta0 (WAD), tauV0, segStart, S, baseOwed, epoch, epochSwapFrac, swapCount, records}; in.state.records "
             "is empty.",
    "out": "out.steps[i] = {state after event i, liveReads (the _tryRef calls during the event: 0 or 1; the gate's live "
           "leg is read only when vestable, and a mint at a stale cache reads tryRef), emitted}. emitted pins the "
           "vault events Checkpoint(tauV, tauB, nav0, delta, base, L, lo, hi), BreachProcessed(count, lastBlock, "
           "forfeited) and CheckpointDeferred() plus the ackBreaches stub, in order; ModeChanged and EpochRecorded "
           "are not pinned here (the mode is in state.bank; the record subset below is all the vectors pin; the "
           "remaining EpochRecord fields are tested by U-mode and the P4 record tests).",
    "records": "records[e] = {base, minted, forfeited, forfeitValue, flags, end (0 until the epoch roll writes it)}: "
               "the subset of EpochRecord the model computes. flags are names; as a u8 the bit of each flag is its "
               "position in the record's flag list (0 stale, 1 witnessMissing, 2 witnessFallback, 3 breach, "
               "4 refStale, 5 deferred, 6 unverified).",
    "notes": SCENARIO_NOTES,
}


def main():
    n = {}
    n["math"] = write("math", MATH_CONV, math_cases())
    n["ledger"] = write("ledger", LEDGER_CONV, ledger_cases())
    n["placement"] = write("placement", PLACEMENT_CONV, placement_cases())
    n["bank"] = write("bank", BANK_CONV, bank_cases())
    n["clock"] = write("clock", CLOCK_CONV, clock_cases())
    n["mode"] = write("mode", MODE_CONV, mode_cases())
    n["scenario"] = write("scenario", SCENARIO_CONV, scenario_cases())
    print("vectors:", ", ".join(f"{k} {v}" for k, v in n.items()), f"-> {OUT} (specVersion {R.SPEC_VERSION})")
    return n


if __name__ == "__main__":
    main()
