// SPDX-License-Identifier: MIT
pragma solidity 0.8.26;

/// @notice The hook's observation, read by the vault at every checkpoint.
/// @dev med9: median of the last 9 block-start ticks (records only); tickSpot: the pool's current tick;
///      refTick / refCachedAt / refFresh: the cached reference sample, the block it was cached at, and whether the
///      cache is fresh (`block < refCachedAt + 2 * GRID`); resetStreak: consecutive in-range anchors of the current run;
///      gapSinceVerify: a span longer than the cache's freshness closed since the last verified return; emptyRange: no
///      range registered; tauV / tauB: in-range block counters of the vault and the witness; outBlocks: observed
///      out-of-range blocks of the current run; breachSeq / pendingBreaches: breaches latched in total and not yet
///      processed; lastPendingBlock / tauVAtLastPending: the last latched crossing block and tauV at that latch;
///      gridIndex = block / GRID; epoch = block / EPOCH.
struct Obs {
    int24 med9;
    int24 tickSpot;
    int24 refTick;
    uint40 refCachedAt;
    bool refFresh;
    uint8 resetStreak;
    bool gapSinceVerify;
    bool emptyRange;
    uint64 tauV;
    uint64 tauB;
    uint64 outBlocks;
    uint32 breachSeq;
    uint32 pendingBreaches;
    uint40 lastPendingBlock;
    uint64 tauVAtLastPending;
    uint32 gridIndex;
    uint32 epoch;
}
