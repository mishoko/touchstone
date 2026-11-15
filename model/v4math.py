"""Integer ports of the Uniswap v4 math libraries used by the Touchstone reference model.

Ported from the upstream libraries (v4-core `FullMath`, `TickMath`, `SqrtPriceMath`, `SwapMath`;
v4-periphery `LiquidityAmounts`), keeping their rounding flags, so that golden vectors are bit-exact.
`get_tick_at_sqrt_price` is implemented by its defining property (the greatest tick whose sqrt price
is <= the input), which the upstream bit-twiddling implementation satisfies exactly.
"""

Q96 = 1 << 96
Q128 = 1 << 128
U128 = (1 << 128) - 1
U160 = (1 << 160) - 1
U256 = (1 << 256) - 1
MIN_TICK = -887272
MAX_TICK = 887272
MIN_SQRT_PRICE = 4295128739
MAX_SQRT_PRICE = 1461446703485210103287273052203988822378723970342
MAX_SWAP_FEE = 1_000_000


class MathRevert(Exception):
    """A revert of a ported library function (the Solidity function would revert)."""


# ---------------------------------------------------------------- FullMath
def mul_div(a, b, d):
    if d == 0:
        raise MathRevert("mulDiv: zero denominator")
    r = (a * b) // d
    if r > U256:
        raise MathRevert("mulDiv: overflow")
    return r


def mul_div_up(a, b, d):
    r = mul_div(a, b, d)
    if (a * b) % d:
        if r == U256:
            raise MathRevert("mulDivRoundingUp: overflow")
        r += 1
    return r


def div_up(a, b):
    """UnsafeMath.divRoundingUp."""
    if b == 0:
        raise MathRevert("divRoundingUp: zero denominator")
    return a // b + (1 if a % b else 0)


def ceil_div(a, b):
    if b == 0:
        raise MathRevert("ceilDiv: zero denominator")
    return -((-a) // b)


# ---------------------------------------------------------------- TickMath
_TICK_CONSTS = [
    (0x2, 0xfff97272373d413259a46990580e213a),
    (0x4, 0xfff2e50f5f656932ef12357cf3c7fdcc),
    (0x8, 0xffe5caca7e10e4e61c3624eaa0941cd0),
    (0x10, 0xffcb9843d60f6159c9db58835c926644),
    (0x20, 0xff973b41fa98c081472e6896dfb254c0),
    (0x40, 0xff2ea16466c96a3843ec78b326b52861),
    (0x80, 0xfe5dee046a99a2a811c461f1969c3053),
    (0x100, 0xfcbe86c7900a88aedcffc83b479aa3a4),
    (0x200, 0xf987a7253ac413176f2b074cf7815e54),
    (0x400, 0xf3392b0822b70005940c7a398e4b70f3),
    (0x800, 0xe7159475a2c29b7443b29c7fa6e889d9),
    (0x1000, 0xd097f3bdfd2022b8845ad8f792aa5825),
    (0x2000, 0xa9f746462d870fdf8a65dc1f90e061e5),
    (0x4000, 0x70d869a156d2a1b890bb3df62baf32f7),
    (0x8000, 0x31be135f97d08fd981231505542fcfa6),
    (0x10000, 0x9aa508b5b7a84e1c677de54f3e99bc9),
    (0x20000, 0x5d6af8dedb81196699c329225ee604),
    (0x40000, 0x2216e584f5fa1ea926041bedfe98),
    (0x80000, 0x48a170391f7dc42444e8fa2),
]
_SQRT_CACHE = {}


def get_sqrt_price_at_tick(tick):
    c = _SQRT_CACHE.get(tick)
    if c is not None:
        return c
    a = abs(tick)
    if a > MAX_TICK:
        raise MathRevert("InvalidTick")
    price = 0xfffcb933bd6fad37aa2d162d1a594001 if a & 1 else 1 << 128
    for bit, k in _TICK_CONSTS:
        if a & bit:
            price = (price * k) >> 128
    if tick > 0:
        price = U256 // price
    r = (price + (1 << 32) - 1) >> 32
    _SQRT_CACHE[tick] = r
    return r


def get_tick_at_sqrt_price(s):
    if s < MIN_SQRT_PRICE or s >= MAX_SQRT_PRICE:
        raise MathRevert("InvalidSqrtPrice")
    lo, hi = MIN_TICK, MAX_TICK
    while lo < hi:  # greatest t with sqrtAt(t) <= s
        mid = (lo + hi + 1) // 2
        if get_sqrt_price_at_tick(mid) <= s:
            lo = mid
        else:
            hi = mid - 1
    return lo


# ---------------------------------------------------------------- SqrtPriceMath
def get_amount0_delta(sa, sb, L, round_up):
    if sa > sb:
        sa, sb = sb, sa
    if sa == 0:
        raise MathRevert("InvalidPrice")
    n1 = L << 96
    n2 = sb - sa
    if round_up:
        return div_up(mul_div_up(n1, n2, sb), sa)
    return mul_div(n1, n2, sb) // sa


def get_amount1_delta(sa, sb, L, round_up):
    if sa > sb:
        sa, sb = sb, sa
    return mul_div_up(L, sb - sa, Q96) if round_up else mul_div(L, sb - sa, Q96)


def _next_from_amount0_up(s, L, amount, add):
    if amount == 0:
        return s
    n1 = L << 96
    if add:
        product = amount * s
        if product <= U256:
            den = n1 + product
            if den <= U256:
                return mul_div_up(n1, s, den)
        return div_up(n1, n1 // s + amount)
    product = amount * s
    if product > U256 or n1 <= product:
        raise MathRevert("PriceOverflow")
    r = mul_div_up(n1, s, n1 - product)
    if r > U160:
        raise MathRevert("SafeCastOverflow")
    return r


def _next_from_amount1_down(s, L, amount, add):
    if add:
        q = (amount << 96) // L if amount <= U160 else mul_div(amount, Q96, L)
        r = s + q
        if r > U160:
            raise MathRevert("SafeCastOverflow")
        return r
    q = div_up(amount << 96, L) if amount <= U160 else mul_div_up(amount, Q96, L)
    if s <= q:
        raise MathRevert("NotEnoughLiquidity")
    return s - q


def get_next_sqrt_price_from_input(s, L, amount_in, zero_for_one):
    if s == 0 or L == 0:
        raise MathRevert("InvalidPriceOrLiquidity")
    return _next_from_amount0_up(s, L, amount_in, True) if zero_for_one else _next_from_amount1_down(s, L, amount_in, True)


# ---------------------------------------------------------------- SwapMath (exact input only)
def compute_swap_step_exact_in(s_cur, s_target, L, amount_remaining, fee_pips):
    """SwapMath.computeSwapStep for an exact-input step (amountRemaining > 0 here = input left).
    Returns (sqrtNext, amountIn, amountOut, feeAmount)."""
    zero_for_one = s_cur >= s_target
    less_fee = mul_div(amount_remaining, MAX_SWAP_FEE - fee_pips, MAX_SWAP_FEE)
    amount_in = get_amount0_delta(s_target, s_cur, L, True) if zero_for_one else get_amount1_delta(s_cur, s_target, L, True)
    if less_fee >= amount_in:
        s_next = s_target
        fee = amount_in if fee_pips == MAX_SWAP_FEE else mul_div_up(amount_in, fee_pips, MAX_SWAP_FEE - fee_pips)
    else:
        amount_in = less_fee
        s_next = get_next_sqrt_price_from_input(s_cur, L, less_fee, zero_for_one)
        fee = amount_remaining - amount_in
    amount_out = get_amount1_delta(s_next, s_cur, L, False) if zero_for_one else get_amount0_delta(s_cur, s_next, L, False)
    return s_next, amount_in, amount_out, fee


# ---------------------------------------------------------------- LiquidityAmounts (v4-periphery)
def _to_u128(x):
    if x > U128:
        raise MathRevert("SafeCastOverflow")
    return x


def get_liquidity_for_amount0(sa, sb, amount0):
    if sa > sb:
        sa, sb = sb, sa
    inter = mul_div(sa, sb, Q96)
    return _to_u128(mul_div(amount0, inter, sb - sa))


def get_liquidity_for_amount1(sa, sb, amount1):
    if sa > sb:
        sa, sb = sb, sa
    return _to_u128(mul_div(amount1, Q96, sb - sa))


def get_liquidity_for_amounts(s, sa, sb, amount0, amount1):
    if sa > sb:
        sa, sb = sb, sa
    if s <= sa:
        return get_liquidity_for_amount0(sa, sb, amount0)
    if s < sb:
        l0 = get_liquidity_for_amount0(s, sb, amount0)
        l1 = get_liquidity_for_amount1(sa, s, amount1)
        return min(l0, l1)
    return get_liquidity_for_amount1(sa, sb, amount1)
