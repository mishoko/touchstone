// SPDX-License-Identifier: MIT
pragma solidity 0.8.26;

/// @notice The optional exact-input swap from idle that a placement runs before it adds liquidity (the same guards and
///         counters as `rebalanceSwap`).
struct PreSwap {
    bool active;
    bool zeroForOne;
    uint128 amountIn;
}
