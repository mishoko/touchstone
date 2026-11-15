"""Golden-vector emitter for the Touchstone reference model.

Writes `test/vectors/{math,ledger,bank,placement}.json`, each
`{schema, specVersion, conventions, cases:[{id, fn, in, out}]}` with every integer as a decimal string.
Action vectors take holdings before and after as inputs; the holdings are produced here by executing
the action against the model's pool simulator, so they are realistic, but the vectors themselves need
no swap math to check.
"""
import json
import os
import random

import reference as R
from v4math import (MIN_TICK, MAX_TICK, MIN_SQRT_PRICE, MAX_SQRT_PRICE, MathRevert, Q96, U256, mul_div,
                    mul_div_up, get_sqrt_price_at_tick as sqrt_at, get_tick_at_sqrt_price, get_amount0_delta,
                    get_amount1_delta, get_liquidity_for_amounts)

OUT = os.environ.get("VECTORS_OUT") or os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "test", "vectors")
SCHEMA = "touchstone-vectors/1"
MIN_CASES = {"math": 40, "ledger": 10, "bank": 30, "placement": 10}

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
            add("LiquidityValue.q", dict(x=x, sqrtPriceX96=s), {"value": R.q(x, s)})
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

    def add(tag, before, after, s_spot, s_ref, S_=S, unv_=unv, bond_=bond, note="", expect=None):
        c, p, n, b, ok = R.measure(before, after, s_spot, s_ref, S_, unv_, bond_)
        if expect:  # the generator asserts that each vector shows what its name says
            leg, positive, ok_want = expect
            assert (p == (s_spot if leg == "spot" else s_ref)), (tag, "p*")
            assert (c > rounding(s_spot)) == positive, (tag, "charge", c)
            assert ok == ok_want, (tag, "ok")
        cases.append(dict(id=f"ledger-{len(cases):03d}-{tag}", fn="ExecLedger.measure",
                          **{"in": dict(before=before.d(), after=after.d(), sqrtSpot=s_spot, sqrtRef=s_ref, S=S_,
                                        unvested=unv_, bond=bond_),
                             "out": dict(charge=c, pStar=p, navAfterPStar=n, burn=b, ok=ok)}))

    def addv(tag, h, s):
        nav, pv = R.value(h, s)
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
    # ExecLedger.value
    for s in [sqrt_at(T0), sqrt_at(T0 + 500), sqrt_at(T0 + 2500), sqrt_at(T0 + 3000)]:
        addv("value", h0, s)
    addv("value-q-high-branch", R.Holdings(5, 7), (1 << 128) + 12345)
    addv("value-empty", R.Holdings(), sqrt_at(T0))
    return cases


LEDGER_CONV = {
    "measure": "loss_p = NAV_before(p) - NAV_after(p) for p in (sqrtSpot, sqrtRef); charge = max(0, max loss); "
               "pStar = argmax, sqrtSpot on a tie (incl. charge 0); navAfterPStar = NAV_after(pStar); "
               "burn = ceilDiv(charge*S, navAfterPStar) (0 when charge = 0; type(uint256).max when "
               "navAfterPStar = 0 and charge > 0); ok = burn <= unvested + bond.",
    "NAV": "NAV(s) = q(idle0 + x_pos(s), s) + idle1 + y_pos(s); positions per Appendix A with roundUp = false.",
    "value": "ExecLedger.value(h, sqrtP) = (NAV(sqrtP), val(x_pos, y_pos, sqrtP)).",
    "holdings": "Holdings {idle0, idle1, L, lo, hi}; before/after are inputs.",
    "fields": "ExecLedger.measure in {before, after, sqrtSpot, sqrtRef, S, unvested, bond} out {charge, pStar, "
              "navAfterPStar, burn, ok}; ExecLedger.value in {h, sqrtP} out {nav, posValue}; Holdings = {idle0, idle1, "
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
    return cases


PLACEMENT_CONV = {
    "check": "errCode = first failing code in numeric order: 1 |tickSpot - refTick| > band; 2 hi - lo != wTicks; "
             "3 lo or hi not a multiple of spacing; 4 not lo <= tickSpot < hi; 5 not lo <= refTick < hi; 0 ok. "
             "The vault maps 1 Fence, 2 Width, 3 Alignment, 4 and 5 NotContained.",
    "size": "L = getLiquidityForAmounts(sqrtSpot, sqrtAt(lo), sqrtAt(hi), idle0, idle1) - (L/1e9 + 1), floored at 0; "
            "after = idle minus the v4 add amounts of L at sqrtSpot (roundUp = true); "
            "deltaAfter = min(WAD, positionValue(sqrtRef)*WAD / NAV(sqrtRef)) of `after` (0 when NAV = 0). "
            "deltaAfter < DELTA_MIN (0.5 WAD) means the vault reverts DeltaMin. Legs saturate at type(uint128).max "
            "(spot one unit inside an edge, case token1-edge-saturated-leg).",
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
    add("charge-exact-total", st(cohorts=[(7, k)], maturedPending=k, bondShares=k), "Charge", dict(burn=3 * k))
    add("charge-insolvent", st(cohorts=[(7, k)], maturedPending=k, bondShares=k), "Charge", dict(burn=3 * k + 1))
    add("charge-zero", st(cohorts=[(7, k)], bondShares=k), "Charge", dict(burn=0))
    # Drip
    pool = dict(forfeitPool=12 * k, forfeitTotal=12 * k, forfeitStart=1000)
    for now in (1000, 1030, 1060, 1119, 1120, 5000):
        add(f"drip-{now}", st(**pool), "Drip", dict(now=now))
    add("drip-partly-burned", st(forfeitPool=9 * k, forfeitTotal=12 * k, forfeitStart=1000), "Drip", dict(now=1060),
        note="target 6k burned so far; 3k already burned; burn 3k")
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
    # Claim / TopUp / Release
    add("claim", st(vestedUnclaimed=7 * k), "Claim", {})
    add("claim-exited", st(vestedUnclaimed=7 * k, mode=R.EXITED), "Claim", {}, note="claim open in every mode")
    add("topup", st(bondShares=k), "TopUp", dict(shares=2 * k))
    ex = dict(mode=R.EXITED, breaches=3, exitBlock=1000, bondShares=9 * k)
    add("release-locked", st(**ex), "Release", dict(shares=9 * k, now=1479))
    add("release-at-delay", st(**ex), "Release", dict(shares=9 * k, now=1480))
    add("release-normal", st(bondShares=9 * k), "Release", dict(shares=9 * k, now=10 ** 6), note="BadMode")
    dm = dict(mode=R.EXITED, breaches=3, exitBlock=10000, bondShares=9 * k, dripBlocks=3600, releaseDelay=14400)
    add("release-demo-minus-1", st(**dm), "Release", dict(shares=9 * k, now=10000 + 14400 - 1),
        note="DEMO: releaseDelay = V·EPOCH = 14400 != dripBlocks 3600")
    add("release-demo-at", st(**dm), "Release", dict(shares=9 * k, now=10000 + 14400))
    b = st(mode=R.EXITED, breaches=3, exitBlock=1000, bondShares=9 * k)
    R.fee_bank_apply(b, "Breach", dict(count=1, now=1400))
    add("release-after-post-exit-breach", b, "Release", dict(shares=9 * k, now=1480),
        note="a post-exit breach does not defer the release")
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
    "Op": "enum Op {Accrue, Breach, VestTime, Drip, Charge, Place, Retire, Claim, TopUp, Release}; in.op is the "
          "ordinal; the case id names the op.",
    "fields": "in {bank, op, args}; out {bank, burnShares, payShares, a, b2} or {revert}; Bank = {cohorts[5] {epoch, "
              "shares}, maturedPending, vestedUnclaimed, bondShares, forfeitPool, forfeitTotal, forfeitStart, "
              "dripBlocks, releaseDelay, breaches, mode, placedByCurator, exitBlock}.",
    "args": "Accrue (epoch, shares, gateOpen); Breach (count, now); VestTime (nowEpoch, gateOpen); Drip (now); "
            "Charge (burn); Place (); Retire (now); Claim (); TopUp (shares); Release (shares, now).",
    "returns": "Charge (bank burned, bond burned); Breach/Retire (forfeited, mode after); VestTime/Accrue (vested, moved "
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


def main():
    n = {}
    n["math"] = write("math", MATH_CONV, math_cases())
    n["ledger"] = write("ledger", LEDGER_CONV, ledger_cases())
    n["placement"] = write("placement", PLACEMENT_CONV, placement_cases())
    n["bank"] = write("bank", BANK_CONV, bank_cases())
    print("vectors:", ", ".join(f"{k} {v}" for k, v in n.items()), f"-> {OUT} (specVersion {R.SPEC_VERSION})")
    return n


if __name__ == "__main__":
    main()
