// SPDX-License-Identifier: MIT
pragma solidity 0.8.26;

import {FullMath} from "@uniswap/v4-core/src/libraries/FullMath.sol";
import {FixedPoint96} from "@uniswap/v4-core/src/libraries/FixedPoint96.sol";
import {SqrtPriceMath} from "@uniswap/v4-core/src/libraries/SqrtPriceMath.sol";
import {TickMath} from "@uniswap/v4-core/src/libraries/TickMath.sol";
import {SafeCast} from "@uniswap/v4-core/src/libraries/SafeCast.sol";
import {Holdings} from "../types/Holdings.sol";
import {LiquidityValue} from "./LiquidityValue.sol";

/// @title Placement
/// @notice The curator placement's guards and sizing, pure. `check` returns the first failing guard; `size` adds the
///         maximum liquidity the idle inventory supports at spot, less a headroom, and reports the deployed fraction at
///         the reference. The legs of the liquidity computation saturate instead of reverting: next to an edge the
///         non-binding leg overflows.
library Placement {
    uint256 internal constant WAD = 1e18;

    /// @notice 0 ok; 1 `|tickSpot − refTick| > band`; 2 `hi − lo ≠ wTicks`; 3 misaligned; 4 spot not in `[lo, hi)`;
    ///         5 reference not in `[lo, hi)`. The first failing code in numeric order.
    function check(int24 lo, int24 hi, int24 tickSpot, int24 refTick, int24 band, int24 wTicks, int24 spacing)
        internal
        pure
        returns (uint8)
    {
        int256 dist = int256(tickSpot) - int256(refTick);
        // [MU-010] the fence on the spot tick, both sides
        if (dist > band || -dist > band) return 1;
        // [MU-012] the width is exactly W
        if (int256(hi) - int256(lo) != int256(wTicks)) return 2;
        if (lo % spacing != 0 || hi % spacing != 0) return 3;
        // [MU-011] [MU-136] [MU-013] the range contains spot, half-open at hi
        if (tickSpot < lo || tickSpot >= hi) return 4;
        if (refTick < lo || refTick >= hi) return 5;
        return 0;
    }

    /// @notice The maximum L at `sqrtSpot` from `idle` on `[lo, hi)`, less `L/1e9 + 1`; the holdings after the add (the
    ///         v4 add amounts, rounded up); and `δ_after = min(WAD, positionValue·WAD/NAV)` at `sqrtRef` (0 if NAV = 0).
    function size(Holdings memory idle, int24 lo, int24 hi, uint160 sqrtSpot, uint160 sqrtRef)
        internal
        pure
        returns (uint128 L, Holdings memory after_, uint64 deltaAfter)
    {
        uint160 sa = TickMath.getSqrtPriceAtTick(lo);
        uint160 sb = TickMath.getSqrtPriceAtTick(hi);
        uint256 l0;
        if (sqrtSpot <= sa) {
            l0 = _liq0(sa, sb, idle.idle0);
        } else if (sqrtSpot < sb) {
            uint256 x = _liq0(sqrtSpot, sb, idle.idle0);
            uint256 y = _liq1(sa, sqrtSpot, idle.idle1);
            l0 = x < y ? x : y;
        } else {
            l0 = _liq1(sa, sb, idle.idle1);
        }
        // [MU-018] the headroom
        uint256 head = l0 / 1e9 + 1;
        L = l0 > head ? SafeCast.toUint128(l0 - head) : 0;
        uint256 a0;
        uint256 a1;
        if (L > 0) {
            if (sqrtSpot <= sa) {
                a0 = SqrtPriceMath.getAmount0Delta(sa, sb, L, true);
            } else if (sqrtSpot < sb) {
                a0 = SqrtPriceMath.getAmount0Delta(sqrtSpot, sb, L, true);
                a1 = SqrtPriceMath.getAmount1Delta(sa, sqrtSpot, L, true);
            } else {
                a1 = SqrtPriceMath.getAmount1Delta(sa, sb, L, true);
            }
        }
        after_ = Holdings(uint128(idle.idle0 - a0), uint128(idle.idle1 - a1), L, lo, hi);
        (uint256 x0, uint256 y0) = LiquidityValue.amounts(L, lo, hi, sqrtRef);
        uint256 pv = LiquidityValue.val(x0, y0, sqrtRef);
        uint256 nav = LiquidityValue.val(uint256(after_.idle0) + x0, uint256(after_.idle1) + y0, sqrtRef);
        if (nav > 0) {
            uint256 d = FullMath.mulDiv(pv, WAD, nav);
            deltaAfter = uint64(d < WAD ? d : WAD);
        }
    }

    /// @dev `getLiquidityForAmount0` with the quotient saturated at `type(uint128).max`, overflow-free:
    ///      `a·inter/(b − a) ≥ 2^128 ⟺ floor(a·inter/2^128) ≥ b − a`, and that floor is < 2^224.
    function _liq0(uint160 sa, uint160 sb, uint256 amount0) private pure returns (uint256) {
        uint256 inter = FullMath.mulDiv(sa, sb, FixedPoint96.Q96);
        uint256 d = uint256(sb) - sa;
        // [MU-137] the leg saturates instead of reverting
        if (FullMath.mulDiv(amount0, inter, 1 << 128) >= d) return type(uint128).max;
        return FullMath.mulDiv(amount0, inter, d); // < 2^128 after the check above
    }

    /// @dev `getLiquidityForAmount1` with the result saturated at `type(uint128).max` (`amount1·2^96 < 2^224`).
    function _liq1(uint160 sa, uint160 sb, uint256 amount1) private pure returns (uint256) {
        uint256 l = FullMath.mulDiv(amount1, FixedPoint96.Q96, uint256(sb) - sa);
        // [MU-181] the token1 leg saturates (≈ 2^134 at the bottom of the tick range)
        return l < type(uint128).max ? l : type(uint128).max;
    }
}
