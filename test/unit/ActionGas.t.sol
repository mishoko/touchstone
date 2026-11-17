// SPDX-License-Identifier: MIT
pragma solidity 0.8.26;

import {console} from "forge-std/console.sol";
import {StateLibrary} from "@uniswap/v4-core/src/libraries/StateLibrary.sol";
import {TickMath} from "@uniswap/v4-core/src/libraries/TickMath.sol";
import {LiquidPoolBase} from "../utils/LiquidPoolBase.sol";
import {Bank} from "../../src/types/Bank.sol";
import {PreSwap} from "../../src/types/PreSwap.sol";

/// @notice G-4 (spec §9.1): a curator action costs at most 1.2M gas in its worst prologue: a pending action's
///         settlement, a matured cohort vested through a live reference read, base minted at the tier's real m, an
///         epoch roll, and for the placement a removal, a pre-swap and a re-add. Measured on the factory-deployed harness
///         (the production hot path plus test seams, so an upper bound), with every touched account cooled first.
contract ActionGasTest is LiquidPoolBase {
    using StateLibrary for *;

    uint256 internal constant G4_MAX = 1_200_000;
    /// @dev The transaction's intrinsic gas and calldata, which `gasleft()` around the call does not see.
    uint256 internal constant INTRINSIC = 21_000 + 16 * 4 * 32 * 8;

    function _setRef(int24 t) internal {
        uint32[] memory s = new uint32[](1);
        int24[] memory k = new int24[](1);
        (s[0], k[0]) = (1, t);
        refPool.setPath(s, k);
    }

    function _worstPrologue() internal {
        _setRef(0);
        hv.placeForTest(-1800, 200);
        advance(tier.COOLDOWN);
        vm.prank(curator);
        hv.setPositions(-1800, 200, PreSwap(false, false, 0), 0, type(uint256).max); // delta0 > 0: base mints
        _keepAlive(tier.H_MARKOUT + 20);
        hv.settle();
        vm.prank(curator);
        hv.rebalanceSwap(true, 1e16, type(uint256).max); // pending; its window closes before the measured action
        Bank memory b = hv.bankFull();
        b.maturedPending += 1e20; // a matured cohort to vest
        b.bondShares -= 1e20;
        hv.setBank(b);
        uint32 e0 = _obs().epoch;
        _keepAlive(tier.H_MARKOUT + tier.EPOCH);
        assertGt(_obs().epoch, e0, "an epoch boundary passed");
        (uint40 tA,,,,) = hv.pendingAction();
        assertGt(tA, 0, "an action is pending settlement");
        vm.cool(address(hv));
        vm.cool(address(hook));
        vm.cool(address(manager));
        vm.cool(address(refPool));
        vm.cool(address(token0));
        vm.cool(address(token1));
    }

    function _record(string memory name, uint256 used) internal {
        console.log(name, used);
        if (vm.envOr("TS_MUTATION_RUN", uint256(0)) == 0) vm.snapshotValue("TouchstoneVault", name, used);
    }

    function _checkWorst() internal view {
        (uint40 tA,,,,) = hv.pendingAction();
        assertEq(tA, vm.getBlockTimestamp(), "the old action settled; the new one is pending");
        assertEq(hv.bankFull().maturedPending, 0, "the matured cohort vested");
    }

    function test_G_4__setPositionsWorstCaseWithinTheCap() public {
        _worstPrologue();
        uint256 S = hv.totalSupply();
        vm.prank(curator);
        uint256 g = gasleft();
        hv.setPositions(-1000, 1000, PreSwap(true, true, 1e16), 0, type(uint256).max);
        uint256 used = g - gasleft() + INTRINSIC;
        _record("G-4 setPositions worst case", used);
        _checkWorst();
        assertTrue(hv.totalSupply() != S, "base minted or shares burned in the call");
        assertLe(used, G4_MAX);
    }

    function test_G_4__rebalanceSwapWorstCaseWithinTheCap() public {
        _worstPrologue();
        vm.prank(curator);
        uint256 g = gasleft();
        hv.rebalanceSwap(true, 1e16, type(uint256).max);
        uint256 used = g - gasleft() + INTRINSIC;
        _record("G-4 rebalanceSwap worst case", used);
        _checkWorst();
        assertLe(used, G4_MAX);
    }
}
