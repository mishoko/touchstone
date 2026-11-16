// SPDX-License-Identifier: MIT
pragma solidity 0.8.26;

import {FullMath} from "@uniswap/v4-core/src/libraries/FullMath.sol";
import {SqrtPriceMath} from "@uniswap/v4-core/src/libraries/SqrtPriceMath.sol";
import {TickMath} from "@uniswap/v4-core/src/libraries/TickMath.sol";

/// @notice Valuation in token1 units at a sqrt price. Exact for idle balances; positions are valued at the amounts a
///         removal would pay (v4's rounding down).
library LiquidityValue {
    /// @notice Token0 amount `x` valued in token1 at sqrt price `s` (floor), without overflow at any valid price.
    function q(uint256 x, uint160 s) internal pure returns (uint256) {
        if (s <= type(uint128).max) return FullMath.mulDiv(uint256(s) * s, x, 1 << 192);
        return FullMath.mulDiv(FullMath.mulDiv(s, s, 1 << 64), x, 1 << 128);
    }

    function val(uint256 x, uint256 y, uint160 s) internal pure returns (uint256) {
        return q(x, s) + y;
    }

    /// @notice The token amounts of liquidity `L` on `[lo, hi)` at sqrt price `s`, rounded down (a removal's payout).
    function amounts(uint128 L, int24 lo, int24 hi, uint160 s) internal pure returns (uint256 a0, uint256 a1) {
        if (L == 0) return (0, 0);
        uint160 sa = TickMath.getSqrtPriceAtTick(lo);
        uint160 sb = TickMath.getSqrtPriceAtTick(hi);
        if (s <= sa) return (SqrtPriceMath.getAmount0Delta(sa, sb, L, false), 0);
        if (s >= sb) return (0, SqrtPriceMath.getAmount1Delta(sa, sb, L, false));
        return (SqrtPriceMath.getAmount0Delta(s, sb, L, false), SqrtPriceMath.getAmount1Delta(sa, s, L, false));
    }
}
