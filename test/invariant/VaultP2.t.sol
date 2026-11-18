// SPDX-License-Identifier: MIT
pragma solidity 0.8.26;

import {IPoolManager} from "@uniswap/v4-core/src/interfaces/IPoolManager.sol";
import {StateLibrary} from "@uniswap/v4-core/src/libraries/StateLibrary.sol";
import {LiquidPoolBase} from "../utils/LiquidPoolBase.sol";
import {VaultHandler} from "./handlers/VaultHandler.sol";
import {Holdings} from "../../src/types/Holdings.sol";
import {Ladder} from "../../src/libraries/Ladder.sol";
import {Bank, RING} from "../../src/types/Bank.sol";
import {TouchstoneVault} from "../../src/TouchstoneVault.sol";
import {VaultView} from "../utils/VaultView.sol";
import {Tier} from "../../src/types/Tier.sol";

using VaultView for TouchstoneVault;

/// @notice The P2 invariants on the minimal handler with a live position (L > 0 from the start):
///         I-1 the vault's ERC-6909 claims and position cover its ledger, and the hook's claims cover its witness
///         reserve; I-2 every withdrawal pays exactly the pro-rata share (idle legs floored, the L leg the
///         PoolManager's rounded-down amounts for floor(L·s/S_pre)); I-10 a withdrawal never reverts outside the
///         allow-list, with the reference broken in every mode.
abstract contract VaultP2Invariants is LiquidPoolBase {
    using StateLibrary for IPoolManager;

    VaultHandler internal handler;

    function setUp() public virtual override {
        super.setUp();
        int24 c = _seedTick();
        hv.placeForTest(c - 600, c + 600);
        (uint256 m0, uint256 m1) = _highTick() ? (uint256(30_000e6), uint256(10e18)) : (uint256(1e20), uint256(1e20));
        handler = new VaultHandler(
            IPoolManager(address(manager)), hv, hook, refPool, token0, token1, key, blocktime, c, m0, m1
        );
        targetContract(address(handler));
    }

    function invariant_I_1__claimsAndPositionCoverTheLedger() public view {
        Holdings memory h = hv.holdings();
        IPoolManager m = IPoolManager(address(manager));
        assertGe(m.balanceOf(vault, uint256(uint160(address(token0)))), h.idle0, "vault claims0 >= idle0");
        assertGe(m.balanceOf(vault, uint256(uint160(address(token1)))), h.idle1, "vault claims1 >= idle1");
        (uint128 liq,,) = m.getPositionInfo(poolId, vault, h.lo, h.hi, bytes32(0));
        assertGe(liq, h.L, "position >= L");
        // the hook's witness reserve (slot 4: _witnessReserve0 | _witnessReserve1 << 128)
        uint256 w = uint256(vm.load(hook, bytes32(uint256(4))));
        assertGe(m.balanceOf(hook, uint256(uint160(address(token0)))), uint128(w), "hook claims0 >= reserve0");
        assertGe(m.balanceOf(hook, uint256(uint160(address(token1)))), w >> 128, "hook claims1 >= reserve1");
    }

    /// @notice F-3 (vault half): the vault holds at least the shares its bank accounts for.
    function invariant_F_3__vaultHeldSharesCoverTheBank() public view {
        Bank memory b = hv.bankFull();
        uint256 sum = uint256(b.maturedPending) + b.vestedUnclaimed + b.bondShares + b.forfeitPool + b.reserveShares;
        for (uint256 k; k < RING; ++k) sum += b.cohorts[k].shares;
        assertGe(v.balanceOf(vault), sum, "vault-held shares >= bank");
    }

    /// @notice F-3, exact in the handler's domain (no unsolicited share transfers): the vault holds exactly the
    ///         shares its bank accounts for, so a checkpoint burn can be neither short nor long.
    function invariant_F_3__vaultHeldSharesEqualTheBankWithoutGifts() public view {
        Bank memory b = hv.bankFull();
        uint256 sum = uint256(b.maturedPending) + b.vestedUnclaimed + b.bondShares + b.forfeitPool + b.reserveShares;
        for (uint256 k; k < RING; ++k) sum += b.cohorts[k].shares;
        assertEq(v.balanceOf(vault), sum, "vault-held shares == bank");
    }

    function invariant_I_2__withdrawPaysTheExactProRataShare() public view {
        assertEq(handler.i2Failures(), 0, handler.i2Why());
    }

    function invariant_U_guard__curatorActionRevertsOnlyOnItsGuards() public view {
        assertEq(handler.actionFailures(), 0, vm.toString(abi.encodePacked(handler.actionSelector())));
    }

    /// @notice Non-vacuity of the random campaign (value review item 6, F-043). A single run may legitimately see every
    ///         curator action revert (an early breach to EXITED), so non-vacuity is a property of the whole campaign:
    ///         with `TS_NV_LOG=<file>` each run appends its counters, and `script/nonvacuity.py` checks the totals
    ///         (run by `script/ci-local.sh` on the 256 x 128 campaign).
    function afterInvariant() public {
        string memory path = vm.envOr("TS_NV_LOG", string(""));
        if (bytes(path).length == 0) return;
        vm.writeLine(
            path,
            string.concat(
                _highTick() ? "tick196k," : "tick0,",
                vm.toString(handler.rebalanceCalls()), ",", vm.toString(handler.curatorSwaps()), ",",
                vm.toString(handler.placeCalls()), ",", vm.toString(handler.curatorPlacements()), ",",
                vm.toString(handler.placementsMoved()), ",", vm.toString(handler.placementsPreSwapped()), ",",
                vm.toString(handler.withdrawalsMintingBase()), ",", vm.toString(handler.claims()), ",",
                vm.toString(handler.topUps()), ",", vm.toString(handler.retires()), ",", vm.toString(handler.releases()), ",",
                vm.toString(handler.pokes()), ",", vm.toString(handler.endgames()), ",", vm.toString(handler.i3DrainChecks()), ",",
                vm.toString(handler.i12Moved()), ",", vm.toString(handler.forfeitedReserveSettles()), ",",
                vm.toString(handler.paidPayouts())
            )
        );
    }

    /// @notice I-3: vested pay falls only by a claim; at a checkpoint a forfeit pool older than its drip (one epoch)
    ///         is empty; EXITED leaves no base owed (the handler checks after every call); no deficit (F-3 above).
    function invariant_I_3__bankDrainsAndVestedPayIsNeverBurned() public view {
        assertEq(handler.i3Failures(), 0, handler.i3Why());
        Bank memory b = hv.bankFull();
        if (b.mode == Ladder.EXITED) assertEq(hv.scalars().baseOwed, 0, "EXITED => baseOwed == 0");
    }

    /// @notice I-12: a flow's NAV0 re-base uses the supply after the prologue (the settlement burn, the drip, the
    ///         base mint): the next NAV0 is post-settlement.
    /// @notice U-record's closure (spec §3.9): the records' share fields account for every unvested share the bank
    ///         gains, loses or vests (see `VaultHandler.closureGap`).
    function invariant_U_record__recordsCloseTheBank() public view {
        assertEq(handler.closureGap(), 0, "records closure");
    }

    function invariant_I_12__nextNav0IsPostSettlement() public view {
        assertEq(handler.i12Failures(), 0, "I-12");
    }

    /// @notice I-16: in EXITED the curator's shares are eventually claimable, burned or released (`endgame`), and the
    ///         P4 entries revert only on their own guards.
    function invariant_I_16__curatorSharesEventuallyClaimedBurnedOrReleased() public view {
        assertEq(handler.i16Failures(), 0, handler.i16Why());
        assertEq(handler.entryFailures(), 0, vm.toString(abi.encodePacked(handler.entrySelector())));
    }

    /// @notice I-16's fixed sequence (spec §9.1): three breaches driven, a curator action right after the third
    ///         reverts (removal pending behind the prologue), a poke removes the curator, a reserve is pending in
    ///         EXITED, more breaches after removal, then the endgame claims, burns and releases everything.
    function test_I_16__exitedCuratorSharesAreClaimedBurnedOrReleased() public {
        for (uint256 i; i < 3; ++i) handler.deposit(i, 5e22);
        for (uint256 k; k < 3; ++k) handler.seedBank(k, 1e20);
        handler.topUp(0); // the curator holds no shares yet: a no-op
        handler.advance(30 * 256 + 10); // a silent jump: the seeded forfeit pool drips
        handler.deposit(0, 1e21); // its prologue burns the drip: the I-12 check sees a moved supply
        for (uint256 i; i < 3; ++i) handler.driveBreach();
        assertEq(_obs().pendingBreaches, 3, "three latched");
        // a curator action right after the third crossing: pending removal reverts it after the prologue
        vm.prank(curator);
        vm.expectRevert(Ladder.BadMode.selector);
        hv.rebalanceSwap(true, 1e6, type(uint256).max);
        handler.poke(true); // processBreach: EXITED
        (,,,,, uint8 mode,) = v.bank();
        assertEq(mode, Ladder.EXITED, "removed");
        handler.pend(1e15, 1e15, 1e16, 1e20, 3e19); // a reserve with a bond part, pending in EXITED
        handler.seedBank(0, 1e20); // unvested pay appearing after removal (forfeited by the next breach)
        handler.releaseBond(); // BondLocked: before the delay
        handler.claim();
        handler.endgame();
        assertEq(handler.endgames(), 1, "the endgame ran");
        assertEq(handler.i16Failures(), 0, handler.i16Why());
        assertEq(handler.entryFailures(), 0, "entries revert only on their guards");
        assertEq(handler.i3Failures(), 0, handler.i3Why());
        assertEq(handler.i12Failures(), 0, "I-12");
        assertGt(hv.balanceOf(curator), 0, "the curator was paid its bond");
        assertGt(handler.i3DrainChecks(), 0, "an I-3 drain check ran past a drip");
        assertGt(handler.i12Moved(), 0, "an I-12 check ran where the prologue moved the supply");
    }

    function invariant_I_10__withdrawNeverRevertsOutsideTheAllowList() public view {
        assertEq(handler.i10Failures(), 0, vm.toString(abi.encodePacked(handler.i10Selector())));
    }

    /// @notice Non-vacuity of the handler: one fixed sequence reaches every state the invariants must see (L > 0
    ///         withdrawals with fees, a broken reference in every mode, breaches, EXITED) with no I-2 or I-10 failure.
    function test_I_10__handlerSequenceReachesEveryStateWithoutFailures() public {
        address a = address(uint160(0xA11CE0));
        for (uint256 i; i < 3; ++i) handler.deposit(i, 5e22);
        for (uint256 k; k < 3; ++k) handler.seedBank(k, 1e20);
        handler.advance(30 * 256 + 10); // a silent jump of 67 blocks (7690 % 100 = 90): the drip runs
        handler.withdraw(1, 1e21 + 1);
        for (uint256 i; i < 6; ++i) {
            handler.swap(i % 2 == 0, type(uint256).max, 300);
            handler.advance(1);
            handler.withdraw(i, 1e21 + i);
        }
        for (uint256 m = 5; m < 10; ++m) {
            handler.breakRef(m);
            handler.advance(99 * 256 + 50); // a silent jump: 25394 % 100 = 94 >= 85, 37 + 99 % 84 = 52 blocks
            handler.withdraw(m, 3e20 + m);
            handler.deposit(m, 1e20);
        }
        assertTrue(_obs().gapSinceVerify, "a silent jump left a gap");
        handler.breakRef(0);
        handler.place(3, 0, 1, 1); // the curator's placement (no move, no pre-swap): a registration clears the gap
        assertEq(handler.curatorPlacements(), 1, "a curator placement");
        handler.seedBank(1, 1e20); // maturedPending: vestable now
        handler.refresh();
        handler.withdraw(2, 1e21 + 5); // the gate reads the live leg and opens
        handler.refresh();
        for (uint256 i; i < 14; ++i) handler.advance(0); // the cooldown: the placement's markout settles first
        // a real curator action, its markout settled by settle() on the window read
        handler.rebalance(true, 1e6, 0); // within the per-action cap on both pools
        assertEq(handler.curatorSwaps(), 1, "a curator action");
        for (uint256 i; i < 14; ++i) handler.advance(0);
        // a placement with the vault's own move (a sub-tick move: move = 1 << 2; a move of whole ticks through this
        // depth meets the per-action cap) and a token0 pre-swap of 1e6 (within
        // the per-action cap on both pools; pre = 1e6 << 8 | 2: divisible by 3, bit 2 clear); it settles the swap's
        // markout first
        handler.place(5, 0, 256_000_002, 4);
        assertEq(handler.curatorPlacements(), 2, "a moved, pre-swapped placement");
        assertEq(handler.placementsMoved(), 1, "moved");
        assertEq(handler.placementsPreSwapped(), 1, "pre-swapped");
        for (uint256 i; i < 13; ++i) handler.advance(0);
        handler.settle();
        for (uint256 i; i < 3; ++i) handler.driveBreach();
        handler.withdraw(0, hv.balanceOf(a) / 2);
        // the markout settlement: a pending action settled by a withdrawal on the read, one by settle() on the
        // fallback (the reference broken), and one left for a withdrawal under a breach
        handler.breakRef(0);
        handler.pend(5e15, 1e15, 1e16, 1e20, 3e19);
        for (uint256 i; i < 13; ++i) handler.advance(0); // 13 snapshotted blocks: due
        handler.withdraw(1, 1e20 + 1);
        handler.pend(1e16, 0, 1e16, 2e20, 0);
        for (uint256 i; i < 13; ++i) handler.advance(0);
        handler.breakRef(6);
        handler.settle();
        handler.breakRef(0);
        (,,,,, uint8 mode,) = v.bank();
        assertEq(mode, Ladder.EXITED, "EXITED reached");
        assertGt(handler.withdrawalsWithL(), 10, "withdrawals with L > 0");
        assertGe(handler.breachesDriven(), 3, "three breaches driven");
        assertEq(handler.bankSeeds(), 4, "bank seeded");
        assertGt(handler.gateOpenVests(), 0, "a withdrawal vested with the gate open (live leg read and in range)");
        assertGt(handler.exitedWithdrawals(), 0, "a withdrawal in EXITED");
        assertEq(handler.pends(), 2, "two pending actions");
        assertEq(handler.withdrawalsSettling(), 1, "a withdrawal settled");
        // on the read: the first placement (by the swap), the swap (by the moved placement), the moved placement (by
        // settle()) and the seam's first pending action (by a withdrawal)
        assertEq(handler.settledByRead(), 4, "settled on the window read");
        assertEq(handler.curatorSwaps(), 1, "a curator action");
        assertEq(handler.settledReal(), 3, "the three real actions settled");
        assertEq(handler.actionFailures(), 0, "actions revert only on their guards");
        assertEq(handler.settledByFallback(), 1, "settled by the fallback");
        assertEq(handler.i2Failures(), 0, handler.i2Why());
        assertEq(handler.i10Failures(), 0, "I-10");
    }
}

contract VaultP2InvariantsTick0 is VaultP2Invariants {}

contract VaultP2InvariantsTick196k is VaultP2Invariants {
    function _highTick() internal pure override returns (bool) {
        return true;
    }
}
