// SPDX-License-Identifier: MIT
pragma solidity 0.8.26;

import {Extsload} from "@uniswap/v4-core/src/Extsload.sol";
import {TouchstoneVault} from "../../src/TouchstoneVault.sol";
import {Holdings} from "../../src/types/Holdings.sol";
import {RING} from "../../src/types/Bank.sol";

/// @notice Test-side reads of the vault's state through `extsload`, at the slots of its storage-layout snapshot
///         (`test/storage/TouchstoneVault.txt`). The production vault has no state views; an off-chain reader or the
///         Lens decodes the same slots. `test_view_slotsMatchStorage` pins these decoders against the harness's Solidity getters.
library VaultView {
    uint256 internal constant SLOT_IDLE = 5; // idle0 (low 128), idle1 (high 128)
    uint256 internal constant SLOT_L = 6; // L (low 128), lo (bits 128..151), hi (bits 152..175)
    uint256 internal constant SLOT_NAV0 = 7; // nav0 (low 128)
    uint256 internal constant SLOT_BANK = 11; // cohorts: 5 slots, then the scalars

    function _ld(TouchstoneVault v, uint256 slot) private view returns (uint256) {
        return uint256(Extsload(address(v)).extsload(bytes32(slot)));
    }

    function holdings(TouchstoneVault v) internal view returns (Holdings memory h) {
        uint256 a = _ld(v, SLOT_IDLE);
        uint256 b = _ld(v, SLOT_L);
        h.idle0 = uint128(a);
        h.idle1 = uint128(a >> 128);
        h.L = uint128(b);
        h.lo = int24(uint24(b >> 128));
        h.hi = int24(uint24(b >> 152));
    }

    function nav0(TouchstoneVault v) internal view returns (uint256) {
        return uint128(_ld(v, SLOT_NAV0));
    }

    function bondShares(TouchstoneVault v) internal view returns (uint256) {
        return uint128(_ld(v, SLOT_BANK + RING + 1));
    }

    /// @notice (cohort shares summed, maturedPending, vestedUnclaimed, bond, forfeitPool, mode, breaches).
    function bank(TouchstoneVault v)
        internal
        view
        returns (
            uint128 cohortShares,
            uint128 maturedPending,
            uint128 vestedUnclaimed,
            uint128 bond,
            uint128 forfeitPool,
            uint8 mode,
            uint32 breaches
        )
    {
        for (uint256 k; k < RING; ++k) {
            cohortShares += uint128(_ld(v, SLOT_BANK + k) >> 32); // {epoch u32, shares u128}
        }
        uint256 s0 = _ld(v, SLOT_BANK + RING);
        uint256 s1 = _ld(v, SLOT_BANK + RING + 1);
        uint256 s3 = _ld(v, SLOT_BANK + RING + 3);
        (maturedPending, vestedUnclaimed) = (uint128(s0), uint128(s0 >> 128));
        (bond, forfeitPool) = (uint128(s1), uint128(s1 >> 128));
        (breaches, mode) = (uint32(s3), uint8(s3 >> 32));
    }

    function bankLimits(TouchstoneVault v) internal view returns (uint40 dripBlocks, uint40 releaseDelay) {
        uint256 s2 = _ld(v, SLOT_BANK + RING + 2); // forfeitTotal u128, forfeitStart u40, dripBlocks u40, releaseDelay u40
        return (uint40(s2 >> 168), uint40(s2 >> 208));
    }
}
