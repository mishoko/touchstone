// SPDX-License-Identifier: MIT
pragma solidity 0.8.26;

/// @notice The epoch record (spec §3.9; six slots). A span is recorded under the epoch it opened in; while the record
///         is open `end = 0` and `tauV`/`tauB` hold the counters' values at `start`, and the roll replaces them by the
///         differences. Values in token1 units; share counts in vault shares. Every field saturates.
struct EpochRecord {
    uint40 start;
    uint40 end;
    int24 med9Start;
    int24 med9End;
    uint64 tauV;
    uint64 tauB;
    uint192 lTau;
    uint8 flags;
    uint96 base;
    uint96 charge;
    uint128 minted;
    uint128 bankBurned;
    uint128 bondBurned;
    uint128 forfeited;
    uint96 forfeitValue;
}

/// @dev Flag bits of `EpochRecord.flags`.
uint8 constant F_STALE = 1;
uint8 constant F_WITNESS_MISSING = 2;
uint8 constant F_WITNESS_FALLBACK = 4;
uint8 constant F_BREACH = 8;
uint8 constant F_REF_STALE = 16;
uint8 constant F_DEFERRED = 32;
uint8 constant F_UNVERIFIED = 64;
