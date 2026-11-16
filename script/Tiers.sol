// SPDX-License-Identifier: MIT
pragma solidity 0.8.26;

import {Tier} from "../src/types/Tier.sol";
import {Addresses} from "./Addresses.sol";

/// @notice One parameter set per tier and chain, and the configuration checks every deploy runs on it.
///         - MAINNET: Ethereum mainnet replay tier (12 s blocks), reference USDC/WETH 0.05%.
///         - DEMO: Unichain Sepolia (1 s blocks); the deploy script overrides REF_POOL and REF_SQRT_PRICE with the
///           mock-token reference pool it creates.
///         - TEST: the forge tier (12 s blocks, short epochs) so that epochs, vesting, breaches and removal are reached
///           within a test run; REF_POOL is the test's MockRefPool.
///         Values marked provisional are fixed at the parameter freeze: `m_perBlock` (here 1%/yr scaled per block),
///         `REF_GAS_CAP` (estimate; sized on a mainnet fork) and REFRESH_GAS_MIN derived from it, and the mainnet
///         REF_SQRT_PRICE.
library Tiers {
    uint256 internal constant WAD = 1e18;
    uint256 internal constant M_YEAR_WAD_PROVISIONAL = 1e16;
    uint32 internal constant REF_GAS_CAP_PROVISIONAL = 200_000;

    error TierInvalid(string reason);

    function mainnet() internal pure returns (Tier memory t, uint256 blocktime) {
        blocktime = 12;
        t = _common(blocktime);
        t.REF_POOL = Addresses.MAINNET_REF_POOL_USDC_WETH_500;
        t.REF_WINDOW = 600;
        t.MIN_CARD = 400;
        t.REF_SQRT_PRICE = 0; // provisional: set at the parameter freeze
        t.GRID_SEC = 3600;
        t.CURE_BLOCKS = 7200;
        t.EPOCH = 50_400;
        t.GRID = 300;
        t.COOLDOWN = 30;
        t.FORFEIT_DRIP_BLOCKS = 50_400;
        t.BOND_MIN = 6e17;
        t.EDGE_DELAY = 300;
    }

    function demo() internal pure returns (Tier memory t, uint256 blocktime) {
        blocktime = 1;
        t = _common(blocktime);
        t.REF_POOL = address(0); // set by the deploy script (mock-token reference pool)
        t.REF_WINDOW = 300;
        t.MIN_CARD = 1000;
        t.REF_SQRT_PRICE = 0; // set by the deploy script
        t.GRID_SEC = 300;
        t.CURE_BLOCKS = 1800;
        t.EPOCH = 3600;
        t.GRID = 300;
        t.COOLDOWN = 30;
        t.FORFEIT_DRIP_BLOCKS = 3600;
        t.BOND_MIN = 6e17;
        t.EDGE_DELAY = 300;
    }

    function testing() internal pure returns (Tier memory t, uint256 blocktime) {
        blocktime = 12;
        t = _common(blocktime);
        t.REF_POOL = address(0); // set by the test (MockRefPool)
        t.REF_WINDOW = 600;
        t.MIN_CARD = 400;
        t.REF_SQRT_PRICE = 0; // set by the test
        t.GRID_SEC = 240;
        t.CURE_BLOCKS = 200;
        t.EPOCH = 120;
        t.GRID = 20;
        t.COOLDOWN = 5;
        t.FORFEIT_DRIP_BLOCKS = 120;
        t.BOND_MIN = 0;
        t.EDGE_DELAY = 20;
    }

    function _common(uint256 blocktime) private pure returns (Tier memory t) {
        t.REF_BAND = 60;
        t.REF_GAS_CAP = REF_GAS_CAP_PROVISIONAL;
        t.REFRESH_GAS_MIN = uint32(uint256(REF_GAS_CAP_PROVISIONAL) * 64 / 63 + 30_000);
        t.m_perBlock = uint64(M_YEAR_WAD_PROVISIONAL * blocktime / 365 days);
        t.BOND_RATIO = 6e14; // 6e-4
        t.MAX_SWAP_ACTION = 2e16; // 2% of NAV
        t.MAX_SWAP_EPOCH = 5e16; // 5% of NAV
        t.MAX_SWAPS = 20;
        t.W_TICKS = 2000;
        t.FEE_TICKS = 5;
        t.BAND_SWAP = 5;
        t.DELTA_MIN = uint64(WAD / 2);
        t.RESET_RUN = 2;
        t.L_W_TARGET = 1000;
    }

    /// @notice The configuration checks: the breach clock's cure window covers the edge bot's wait, two grids of
    ///         cache freshness, the reference window and one grid; a reset needs at least two anchors; the block grid
    ///         and the timestamp grid agree; an epoch is a whole number of grids.
    function check(Tier memory t, uint256 blocktime) internal pure {
        if (blocktime == 0 || t.GRID == 0 || t.REF_WINDOW == 0) revert TierInvalid("zero");
        if (uint256(t.CURE_BLOCKS) < uint256(t.EDGE_DELAY) + 2 * uint256(t.GRID) + t.REF_WINDOW / blocktime + t.GRID) {
            revert TierInvalid("CURE_BLOCKS");
        }
        if (t.RESET_RUN < 2) revert TierInvalid("RESET_RUN");
        if (uint256(t.GRID) * blocktime != t.GRID_SEC) revert TierInvalid("GRID_SEC");
        if (t.EPOCH % t.GRID != 0) revert TierInvalid("EPOCH");
    }
}
