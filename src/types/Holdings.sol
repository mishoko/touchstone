// SPDX-License-Identifier: MIT
pragma solidity 0.8.26;

/// @notice The vault's holdings: idle balances (ERC-6909 claims) and its one position.
struct Holdings {
    uint128 idle0;
    uint128 idle1;
    uint128 L;
    int24 lo;
    int24 hi;
}
