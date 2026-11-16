// SPDX-License-Identifier: MIT
pragma solidity 0.8.26;

// Vesting epochs (compile-time constant); the cohort ring has V + 1 slots keyed by `epoch mod (V + 1)`.
uint256 constant V = 4;
uint256 constant RING = V + 1;

struct Cohort {
    uint32 epoch;
    uint128 shares;
}

/// @notice The curator's bank. Every bucket is a count of vault shares held by the vault contract. The reserve
///         (`reserveShares`) is outside every bucket: vesting, claim and release cannot reach it.
struct Bank {
    Cohort[RING] cohorts;
    uint128 maturedPending;
    uint128 vestedUnclaimed;
    uint128 bondShares;
    uint128 forfeitPool;
    uint128 forfeitTotal;
    uint40 forfeitStart;
    uint40 dripBlocks;
    uint40 releaseDelay;
    uint32 breaches;
    uint8 mode;
    bool placedByCurator;
    uint40 exitBlock;
    uint128 reserveShares;
    uint128 reserveBond;
    bool reserveForfeit;
}

/// @notice FeeBank operations. Arguments are `abi.encode`d per op (see `FeeBank.apply`).
enum Op {
    Accrue,
    Breach,
    VestTime,
    Drip,
    Charge,
    Place,
    Retire,
    Claim,
    TopUp,
    Release,
    Settle
}
