// SPDX-License-Identifier: MIT
pragma solidity 0.8.26;

/// @notice The pending curator action's record beyond its two holdings: the immediate charge and the reserve (value,
///         token1 units), the spots the settlement's fee terms start from (the pre-move spot `s0` and the spot the
///         action left, `sEnd`), the charge's burn and the supply just before it (shares).
struct Markout {
    uint128 charge0;
    uint128 reserve;
    uint160 s0;
    uint160 sEnd;
    uint128 burn0;
    uint128 sAct;
}
