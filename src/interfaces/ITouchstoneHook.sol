// SPDX-License-Identifier: MIT
pragma solidity 0.8.26;

import {Obs} from "../types/Obs.sol";

interface ITouchstoneHook {
    event RefRefreshed(uint32 grid, int24 tick);
    event RefReadFailed(uint32 grid);
    event BreachLatched(uint32 seq, uint40 block);
    event RangeRegistered(int24 lo, int24 hi, int24 liveTick);
    event GapSet(uint40 block);
    /// @param kind 0 = registration, 1 = reset after RESET_RUN consecutive in-range anchors
    event GapCleared(uint8 kind);
    event WitnessRolled(uint32 epoch, uint256 fg0, uint256 fg1, uint128 lw);

    error WrongPool();
    error OnlyVault();
    error RefUnreadable();
    error RefGas();
    /// @notice The tier fails a constructor check (REF_WINDOW = 0, GRID = 0 or EPOCH not a multiple of GRID).
    error BadTier();
    /// @notice The reference pool fails a constructor check (token order or observation cardinality).
    error BadRef();
    /// @notice `armAtSeed` was already called.
    error Armed();

    function snapshotIfNewBlock() external returns (Obs memory);
    function obs() external view returns (Obs memory);
    function liveRef() external view returns (int24 refTick);
    function tryRef() external view returns (bool ok, int24 tick);
    function tryWindow(uint32 a, uint32 b) external view returns (bool ok, int24 tick);
    function refreshRef() external;
    function epochStartRef() external view returns (int24);
    function frozen(uint32 gridIndex) external view returns (int24 med9, uint64 tauV, uint64 tauB, bool exists);

    function setVaultRange(int24 lo, int24 hi, int24 liveTick) external;
    function armAtSeed() external;
    function ackBreaches(uint32 n) external;
    function fundWitness(uint256 a0, uint256 a1) external;
    function rollWitness(uint32 epoch) external returns (uint256 fg0, uint256 fg1, uint128 lw, bool fallbackUsed);
}
