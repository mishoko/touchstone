// SPDX-License-Identifier: MIT
pragma solidity 0.8.26;

import {HarnessVaultBase} from "../utils/HarnessVaultBase.sol";
import {VaultView} from "../utils/VaultView.sol";
import {TouchstoneVault} from "../../src/TouchstoneVault.sol";
import {Holdings} from "../../src/types/Holdings.sol";
import {Bank, Cohort, RING} from "../../src/types/Bank.sol";

using VaultView for TouchstoneVault;

/// @notice The vault has no state views (the views moved to the Lens): off-chain readers decode its storage through
///         `extsload`. These tests pin the test-side decoders against the compiler's own layout, read by the harness's
///         Solidity getters on the same state, with every field distinct, and the tier commitment against the tier.
contract VaultViewTest is HarnessVaultBase {
    function test_view_slotsMatchStorage() public {
        Bank memory b;
        for (uint256 k; k < RING; ++k) {
            b.cohorts[k] = Cohort(uint32(100 + k), uint128(1e20 + 7 * k + 1));
        }
        (b.maturedPending, b.vestedUnclaimed, b.bondShares, b.forfeitPool) = (11, 13, 17e21, 19);
        (b.forfeitTotal, b.forfeitStart, b.dripBlocks, b.releaseDelay) = (23, 29, 31, 37);
        (b.breaches, b.mode, b.placedByCurator, b.exitBlock) = (2, 1, true, 41);
        (b.reserveShares, b.reserveBond, b.reserveForfeit) = (43, 47, true);
        hv.setBank(b);
        hv.setHoldings(Holdings(type(uint128).max - 3, 5, 7e18, -887_220, 887_210));

        TouchstoneVault tv = TouchstoneVault(address(hv));
        Holdings memory got = tv.holdings();
        assertEq(keccak256(abi.encode(got)), keccak256(abi.encode(hv.holdings())), "holdings");
        assertEq(tv.nav0(), hv.nav0(), "nav0");
        assertEq(tv.bondShares(), hv.bondShares(), "bond");
        (uint128 c, uint128 mp, uint128 vu, uint128 bo, uint128 fp, uint8 m, uint32 br) = tv.bank();
        (uint128 c2, uint128 mp2, uint128 vu2, uint128 bo2, uint128 fp2, uint8 m2, uint32 br2) = hv.bank();
        assertEq(abi.encode(c, mp, vu, bo, fp, m, br), abi.encode(c2, mp2, vu2, bo2, fp2, m2, br2), "bank");
        hv.setPending(
            0xABCDEF12, Holdings(101, 103, 107, -109, 113), Holdings(127, 131, 137, -139, 149), 151, 157
        );
        (uint40 tA, Holdings memory pb, Holdings memory pa, uint128 c0, uint128 r) = tv.pending();
        (uint40 tA2, Holdings memory pb2, Holdings memory pa2, uint128 c02, uint128 r2) = hv.pendingAction();
        assertEq(abi.encode(tA, pb, pa, c0, r), abi.encode(tA2, pb2, pa2, c02, r2), "pending");
        assertEq(tA, 0xABCDEF12, "tAction decoded");
        hv.setScalars(1e30, 0x1234_5678_9ABC, 3, 4, 5, 0xDEADBEEF, 0x0FED_CBA9_8765_4321, 0xBEEF);
        hv.setLastAction(0xCAFE_F00D_12);
        (uint32 ep, uint64 esf, uint16 sc, uint40 la, uint64 d0) = tv.counters();
        assertEq(ep, hv.scalars().epoch, "epoch");
        assertEq(esf, hv.scalars().epochSwapFrac, "epochSwapFrac");
        assertEq(sc, hv.scalars().swapCount, "swapCount");
        assertEq(la, hv.lastAction(), "lastAction");
        assertEq(d0, hv.scalars().delta0, "delta0");
        assertEq(abi.encode(ep, esf, sc, la), abi.encode(uint32(0xDEADBEEF), uint64(0x0FED_CBA9_8765_4321), uint16(0xBEEF), uint40(0xCAFE_F00D_12)), "distinct values");
        (tA,,,,) = tv.pending();
        assertEq(tA, 0xABCDEF12, "tAction unchanged by the neighbours");
        (uint40 drip, uint40 rel) = tv.bankLimits();
        assertEq(drip, 31, "dripBlocks");
        assertEq(rel, 37, "releaseDelay");
    }

    /// @notice The vault commits to its tier by hash (the factory checks it at deploy).
    function test_view_tierHashCommitsToTheTier() public view {
        assertEq(hv.TIER_HASH(), keccak256(abi.encode(hv.tier())), "TIER_HASH");
        assertEq(hv.TIER_HASH(), keccak256(abi.encode(tier)), "the deployed tier");
    }
}
