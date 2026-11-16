// SPDX-License-Identifier: MIT
pragma solidity 0.8.26;

import {IPoolManager} from "@uniswap/v4-core/src/interfaces/IPoolManager.sol";
import {PoolId} from "@uniswap/v4-core/src/types/PoolId.sol";
import {StateLibrary} from "@uniswap/v4-core/src/libraries/StateLibrary.sol";
import {SqrtPriceMath} from "@uniswap/v4-core/src/libraries/SqrtPriceMath.sol";
import {TickMath} from "@uniswap/v4-core/src/libraries/TickMath.sol";
import {FullMath} from "@uniswap/v4-core/src/libraries/FullMath.sol";
import {FixedPoint128} from "@uniswap/v4-core/src/libraries/FixedPoint128.sol";

/// @notice Independent expectations for the vault's pro-rata flows, computed from the PoolManager's own state and
///         math (never from the vault's code): the fees a modify would pay out, and the token amounts of a liquidity
///         addition (rounded up) or removal (rounded down) at the current price.
library ExpectedFlows {
    using StateLibrary for IPoolManager;

    /// @notice The fees accrued to `owner`'s position since its last modify (what collect-first sweeps).
    function fees(IPoolManager m, PoolId id, address owner, int24 lo, int24 hi)
        internal
        view
        returns (uint256 f0, uint256 f1)
    {
        (uint128 liq, uint256 last0, uint256 last1) = m.getPositionInfo(id, owner, lo, hi, bytes32(0));
        if (liq == 0) return (0, 0);
        (uint256 g0, uint256 g1) = m.getFeeGrowthInside(id, lo, hi);
        unchecked {
            f0 = FullMath.mulDiv(g0 - last0, liq, FixedPoint128.Q128);
            f1 = FullMath.mulDiv(g1 - last1, liq, FixedPoint128.Q128);
        }
    }

    /// @notice The token amounts of `dL` liquidity on [lo, hi) at the pool's current price, with `roundUp` as the
    ///         PoolManager applies it (up for an addition, down for a removal).
    function legs(IPoolManager m, PoolId id, int24 lo, int24 hi, uint128 dL, bool roundUp)
        internal
        view
        returns (uint256 a0, uint256 a1)
    {
        if (dL == 0) return (0, 0);
        (uint160 sp, int24 tick,,) = m.getSlot0(id);
        uint160 sa = TickMath.getSqrtPriceAtTick(lo);
        uint160 sb = TickMath.getSqrtPriceAtTick(hi);
        if (tick < lo) return (SqrtPriceMath.getAmount0Delta(sa, sb, dL, roundUp), 0);
        if (tick >= hi) return (0, SqrtPriceMath.getAmount1Delta(sa, sb, dL, roundUp));
        return (SqrtPriceMath.getAmount0Delta(sp, sb, dL, roundUp), SqrtPriceMath.getAmount1Delta(sa, sp, dL, roundUp));
    }
}
