// SPDX-License-Identifier: MIT
pragma solidity 0.8.26;

/// @notice The vault's entry points, as dispatched inside its `unlock` and gated by `Ladder.allowed`.
enum Entry {
    Deposit,
    Withdraw,
    SetPositions,
    RebalanceSwap,
    Claim,
    TopUpBond,
    Retire,
    ReleaseBond,
    Poke,
    ProcessBreach,
    Seed,
    Settle
}
