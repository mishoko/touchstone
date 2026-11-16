// SPDX-License-Identifier: MIT
pragma solidity 0.8.26;

import {Vm} from "forge-std/Vm.sol";
import {HarnessVaultBase} from "../utils/HarnessVaultBase.sol";
import {ITouchstoneHook} from "../../src/interfaces/ITouchstoneHook.sol";
import {Bank, Cohort} from "../../src/types/Bank.sol";
import {Ladder} from "../../src/libraries/Ladder.sol";

/// @notice The checkpoint in the real flow (factory deployment, real hook, scripted reference pool): breaches latched
///         by an unplaced vault are processed by any entry point; the mode gate runs after the prologue; the vest
///         gate's live leg reads the reference through the hook.
contract VaultCheckpointLiveTest is HarnessVaultBase {
    function setUp() public override {
        super.setUp();
        _fundUser(alice, 1e24);
        _deposit(alice, 1e23);
    }

    function test_U_breach__anEmptyVaultBreachIsProcessedByADeposit() public {
        _driveBreaches(1);
        assertEq(_obs().pendingBreaches, 1, "latched, not processed");
        _deposit(alice, 1e21);
        (,,,,, uint8 mode, uint32 breaches) = v.bank();
        assertEq(breaches, 1, "the deposit's checkpoint processed the breach");
        assertEq(mode, Ladder.BREACHED, "BREACHED");
        assertEq(_obs().pendingBreaches, 0, "acknowledged to the hook");
    }

    function test_U_breach__anEmptyVaultBreachIsProcessedByAWithdrawal() public {
        _driveBreaches(1);
        _withdraw(alice, 1e21);
        (,,,,, uint8 mode, uint32 breaches) = v.bank();
        assertEq(breaches, 1, "the withdrawal's checkpoint processed the breach");
        assertEq(mode, Ladder.BREACHED, "BREACHED");
        assertEq(_obs().pendingBreaches, 0, "acknowledged");
    }

    function test_U_breach__anEmptyVaultReachesExitedAndWithdrawalsStayOpen() public {
        _driveBreaches(3);
        assertEq(_obs().pendingBreaches, 3, "three pending");
        uint256 bal = v.balanceOf(alice);
        (uint256 a0, uint256 a1) = _withdraw(alice, bal / 2);
        assertGt(a0 + a1, 0, "paid in kind");
        (,,,,, uint8 mode, uint32 breaches) = v.bank();
        assertEq(mode, Ladder.EXITED, "removed after three breaches");
        assertEq(breaches, 3, "three");
        vm.expectRevert(Ladder.BadMode.selector);
        _deposit(alice, 1e21);
        _withdraw(alice, v.balanceOf(alice)); // withdrawals stay open in EXITED
        assertEq(v.balanceOf(alice), 0, "fully out");
    }

    function test_U_mode__pendingRemovalRevertsADepositAfterThePrologue() public {
        _driveBreaches(3);
        (,,,,, uint8 before,) = v.bank();
        assertEq(before, Ladder.NORMAL, "still NORMAL before any checkpoint");
        vm.expectRevert(Ladder.BadMode.selector);
        _deposit(alice, 1e21);
        assertEq(_obs().pendingBreaches, 3, "the reverted call left the breaches pending");
        _withdraw(alice, 1e21);
        (,,,,, uint8 mode,) = v.bank();
        assertEq(mode, Ladder.EXITED, "the next checkpoint removes the curator");
    }

    // ------------------------------------------------------------------ the live leg through the hook

    function test_U_vest_gate__realVaultReadsTheLiveReferenceThroughTheHook() public {
        uint256 cohort = _placeAndMature();
        refPool.setTick(5000); // the live TWAP leaves the range; the cached anchor sample stays in range
        advance(15);
        assertTrue(_obs().refFresh, "cache fresh");
        assertEq(_obs().refTick, 0, "cache in range");
        _deposit(alice, 1e21);
        Bank memory b = hv.bankFull();
        assertEq(b.vestedUnclaimed, 0, "no vest while the live reference is out");
        assertEq(b.maturedPending, cohort, "matured cohort waits");
    }

    function test_U_vest_gate__realVaultControlLiveInRangeVests() public {
        uint256 cohort = _placeAndMature();
        advance(15);
        _deposit(alice, 1e21);
        Bank memory b = hv.bankFull();
        assertEq(b.vestedUnclaimed, cohort, "vests with the live reference in range");
        assertEq(b.maturedPending, 0, "nothing pending");
    }

    /// @dev Places [-600, 600) around tick 0, writes an epoch-0 cohort, keeps the clock observed until the cohort is
    ///      due, and stops right after an anchor refresh (so the cache holds for the rest of the anchor).
    function _placeAndMature() internal returns (uint256 shares) {
        hv.placeForTest(-600, 600);
        shares = 7e18;
        Bank memory b = hv.bankFull();
        b.cohorts[0] = Cohort(0, uint128(shares));
        b.bondShares -= uint128(shares);
        hv.setBank(b);
        _keepAlive(600);
        while (vm.getBlockTimestamp() % tier.GRID_SEC >= blocktime * 2) advance(1);
        ITouchstoneHook(hook).refreshRef();
        assertGe(vm.getBlockNumber(), 600, "due");
        assertFalse(_obs().gapSinceVerify, "no gap");
    }
}
