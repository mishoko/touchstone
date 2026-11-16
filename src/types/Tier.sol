// SPDX-License-Identifier: MIT
pragma solidity 0.8.26;

/// @notice Per-tier, per-chain parameters. The vault reads them from `factory.pending()` in its constructor and the
///         hook receives them in its constructor arguments; both store them as immutables.
/// @dev Units: seconds for *_SEC, REF_WINDOW and H_MARKOUT; blocks for CURE_BLOCKS, EPOCH, GRID, COOLDOWN,
///      FORFEIT_DRIP_BLOCKS, EDGE_DELAY; ticks for REF_BAND, W_TICKS, FEE_TICKS, BAND_SWAP; WAD (1e18) fractions for
///      m_perBlock, BOND_RATIO, RESERVE_RATIO, MAX_SWAP_ACTION, MAX_SWAP_EPOCH, DELTA_MIN; token1 units for BOND_MIN and
///      NAV_MAX; liquidity units for L_W_TARGET.
struct Tier {
    address REF_POOL;
    uint32 REF_WINDOW;
    int24 REF_BAND;
    uint32 REF_GAS_CAP;
    uint32 REFRESH_GAS_MIN;
    uint16 MIN_CARD;
    uint160 REF_SQRT_PRICE;
    uint32 GRID_SEC;
    uint32 CURE_BLOCKS;
    uint32 EPOCH;
    uint32 GRID;
    uint32 COOLDOWN;
    uint32 FORFEIT_DRIP_BLOCKS;
    uint64 m_perBlock;
    uint64 BOND_RATIO;
    uint128 BOND_MIN;
    uint128 NAV_MAX;
    uint64 RESERVE_RATIO;
    uint32 H_MARKOUT;
    uint64 MAX_SWAP_ACTION;
    uint64 MAX_SWAP_EPOCH;
    uint16 MAX_SWAPS;
    int24 W_TICKS;
    int24 FEE_TICKS;
    int24 BAND_SWAP;
    uint64 DELTA_MIN;
    uint32 EDGE_DELAY;
    uint8 RESET_RUN;
    uint128 L_W_TARGET;
}
