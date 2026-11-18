// SPDX-License-Identifier: MIT
pragma solidity 0.8.26;

import {Vm} from "forge-std/Vm.sol";
import {HarnessVaultBase} from "../utils/HarnessVaultBase.sol";
import {Scalars} from "../utils/VaultHarness.sol";
import {ITouchstoneHook} from "../../src/interfaces/ITouchstoneHook.sol";
import {IVaultEntries} from "../../src/interfaces/ITouchstoneVaultExtension.sol";
import {Bank, Cohort, RING, V} from "../../src/types/Bank.sol";
import {Ladder} from "../../src/libraries/Ladder.sol";
import {FeeBank} from "../../src/libraries/FeeBank.sol";
import {VaultCommon} from "../../src/TouchstoneVaultBase.sol";
import {MockRefPool} from "../utils/MockRefPool.sol";

/// @notice The six P4 entry points through the vault's fallback and its extension (spec §3.7, §4.5, §4.6): `claim`,
///         `topUpBond`, `retire`, `releaseBond`, `poke`, `processBreach`. Real flow (factory deployment, real hook,
///         scripted reference pool, TEST tier).
contract VaultP4EntriesTest is HarnessVaultBase {
    address internal mallory = makeAddr("mallory");
    /// @dev A deferred base well below the TEST vault's NAV (≈ 2e18 token1 units), so a priced checkpoint mints it.
    uint128 internal constant OWED = 1e15;

    function setUp() public override {
        super.setUp();
        _fundUser(alice, 1e24);
        _deposit(alice, 1e23);
    }

    function _ve() internal view returns (IVaultEntries) {
        return IVaultEntries(vault);
    }

    /// @dev Moves `c` bond shares into an epoch-`e` cohort, `mp` into maturedPending and `vu` into vestedUnclaimed
    ///      (the vault's held balance is unchanged: every bucket is a share the vault holds).
    function _plant(uint32 e, uint128 c, uint128 mp, uint128 vu) internal {
        Bank memory b = hv.bankFull();
        b.cohorts[e % RING] = Cohort(e, c);
        b.maturedPending += mp;
        b.vestedUnclaimed += vu;
        b.bondShares -= c + mp + vu;
        hv.setBank(b);
    }

    function _setBaseOwed(uint128 owed) internal {
        Scalars memory s = hv.scalars();
        hv.setScalars(s.nav0, s.delta0, s.tauV0, s.segStart, owed, s.epoch, s.epochSwapFrac, s.swapCount);
    }

    function _cohortSum() internal view returns (uint256 u) {
        Bank memory b = hv.bankFull();
        for (uint256 k; k < RING; ++k) {
            u += b.cohorts[k].shares;
        }
    }

    function _retire() internal {
        vm.prank(curator);
        _ve().retire();
    }

    // ------------------------------------------------------------------ retire (S16B-05, S18D-01)

    /// @notice A deferred base then `retire()`: `baseOwed` is zeroed, every cohort and `maturedPending` join the
    ///         forfeit pool, and the next priced checkpoint (in EXITED) mints nothing. The retire's own checkpoint
    ///         defers (stale cache, unreadable reference), so the zeroing in `retire` is what keeps the amount out.
    function test_U_forfeit__retireForfeitsADeferredBase() public {
        hv.placeForTest(-600, 600);
        _plant(0, 5e18, 2e18, 0);
        _setBaseOwed(OWED);
        // the cache goes stale and the live read fails: the retire's checkpoint defers (precondition: it did)
        advance(3 * tier.GRID);
        refPool.setMode(MockRefPool.Mode.Revert);
        vm.recordLogs();
        _retire();
        Vm.Log[] memory logs = vm.getRecordedLogs();
        bool deferred;
        for (uint256 i; i < logs.length; ++i) {
            if (logs[i].emitter == vault && logs[i].topics[0] == VaultCommon.CheckpointDeferred.selector) deferred = true;
        }
        assertTrue(deferred, "precondition: the retire's own checkpoint deferred the base");
        Bank memory b = hv.bankFull();
        assertEq(b.mode, Ladder.EXITED, "EXITED");
        assertEq(hv.scalars().baseOwed, 0, "retire zeroes baseOwed");
        assertEq(_cohortSum() + b.maturedPending, 0, "every unvested bucket forfeited");
        assertEq(b.forfeitPool, 7e18, "cohorts and maturedPending in the drip");
        assertFalse(b.placedByCurator, "placedByCurator cleared");
        // a priced checkpoint in EXITED mints nothing
        refPool.setMode(MockRefPool.Mode.Ok);
        advance(1);
        ITouchstoneHook(hook).refreshRef();
        uint256 supply = v.totalSupply();
        _ve().poke();
        assertLe(v.totalSupply(), supply, "no mint after retire");
        assertEq(_cohortSum(), 0, "no cohort written in EXITED");
    }

    /// @notice Control: a `baseOwed` left in place after retire (the zeroing absent) IS minted into a cohort by the
    ///         next priced checkpoint in EXITED, which `claim` (open in EXITED) would pay once vested.
    function test_U_forfeit__ctlBaseOwedLeftAfterRetireIsMintedInExited() public {
        hv.placeForTest(-600, 600);
        _retire();
        _setBaseOwed(OWED);
        advance(1);
        ITouchstoneHook(hook).refreshRef();
        uint256 supply = v.totalSupply();
        _ve().poke();
        assertGt(v.totalSupply(), supply, "minted in EXITED");
        assertGt(_cohortSum(), 0, "into a curator cohort");
    }

    /// @notice Retire: curator only, NORMAL or BREACHED only, burns the drip owed first, emits `Retired(forfeited)`,
    ///         leaves vested-unclaimed pay and the bond in place, and sets `exitBlock`.
    function test_U_mode__retireIsCuratorOnlyAndClosedInExited() public {
        _plant(0, 4e18, 0, 1e18);
        vm.prank(mallory);
        vm.expectRevert(VaultCommon.OnlyCurator.selector);
        _ve().retire();
        uint128 bond = hv.bankFull().bondShares;
        vm.recordLogs();
        _retire();
        Vm.Log[] memory logs = vm.getRecordedLogs();
        bool seen;
        for (uint256 i; i < logs.length; ++i) {
            if (logs[i].emitter == vault && logs[i].topics[0] == VaultCommon.Retired.selector) {
                assertEq(abi.decode(logs[i].data, (uint256)), 4e18, "Retired(forfeited)");
                seen = true;
            }
        }
        assertTrue(seen, "Retired emitted by the vault");
        Bank memory b = hv.bankFull();
        assertEq(b.vestedUnclaimed, 1e18, "vested pay is kept");
        assertEq(b.bondShares, bond, "the bond is untouched");
        assertEq(b.exitBlock, uint40(block.number), "exitBlock");
        vm.prank(curator);
        vm.expectRevert(Ladder.BadMode.selector);
        _ve().retire();
    }

    // ------------------------------------------------------------------ claim, topUpBond, releaseBond (U-4, I-5)

    /// @notice `claim` pays exactly `vestedUnclaimed` to the curator, in every mode; the vault's held balance falls by
    ///         the same amount (custody, F-3's vault form). A non-curator reverts `OnlyCurator`.
    function test_U_4__claimPaysVestedSharesInEveryMode() public {
        _plant(0, 0, 0, 2e18);
        vm.prank(mallory);
        vm.expectRevert(VaultCommon.OnlyCurator.selector);
        _ve().claim();
        uint256 held = v.balanceOf(vault);
        vm.prank(curator);
        _ve().claim();
        assertEq(v.balanceOf(curator), 2e18, "paid");
        assertEq(held - v.balanceOf(vault), 2e18, "the vault's held balance falls by the payment");
        assertEq(hv.bankFull().vestedUnclaimed, 0, "cleared");
        // BREACHED, then EXITED
        _driveBreaches(1);
        _plant(0, 0, 0, 1e18);
        vm.prank(curator);
        _ve().claim();
        assertEq(hv.bankFull().mode, Ladder.BREACHED, "BREACHED");
        assertEq(v.balanceOf(curator), 3e18, "claimed in BREACHED");
        _retire();
        _plant(0, 0, 0, 1e18);
        vm.prank(curator);
        _ve().claim();
        assertEq(v.balanceOf(curator), 4e18, "claimed in EXITED");
    }

    /// @notice `topUpBond` moves the curator's own shares into the bond (the held balance and the bond rise by the
    ///         same amount); curator only; reverts `BadMode` in EXITED.
    function test_U_4__topUpBondMovesTheCuratorsSharesIntoTheBond() public {
        _fundUser(curator, 1e24);
        _deposit(curator, 1e21);
        uint128 bond = hv.bankFull().bondShares;
        uint256 held = v.balanceOf(vault);
        vm.prank(mallory);
        vm.expectRevert(VaultCommon.OnlyCurator.selector);
        _ve().topUpBond(1e20);
        vm.prank(curator);
        _ve().topUpBond(4e20);
        assertEq(hv.bankFull().bondShares, bond + 4e20, "bond");
        assertEq(v.balanceOf(vault) - held, 4e20, "held balance");
        assertEq(v.balanceOf(curator), 1e21 - 4e20, "from the curator");
        _retire();
        vm.prank(curator);
        vm.expectRevert(Ladder.BadMode.selector);
        _ve().topUpBond(1e20);
    }

    /// @notice `releaseBond`: `BadMode` outside EXITED, `BondLocked` before `exitBlock + V·EPOCH`, then the whole bond
    ///         to the curator (the held balance falls by it); a second call pays nothing.
    function test_U_4__releaseBondAfterTheDelayOnlyInExited() public {
        vm.prank(curator);
        vm.expectRevert(Ladder.BadMode.selector);
        _ve().releaseBond();
        _retire();
        uint40 exitBlock = hv.bankFull().exitBlock;
        uint40 delay = uint40(V * tier.EPOCH);
        advance(delay - 1);
        vm.prank(curator);
        vm.expectRevert(FeeBank.BondLocked.selector);
        _ve().releaseBond();
        vm.roll(exitBlock + delay);
        vm.warp(block.timestamp + blocktime);
        uint128 bond = hv.bankFull().bondShares;
        uint256 held = v.balanceOf(vault);
        vm.prank(mallory);
        vm.expectRevert(VaultCommon.OnlyCurator.selector);
        _ve().releaseBond();
        vm.prank(curator);
        _ve().releaseBond();
        assertEq(v.balanceOf(curator), bond, "the bond to the curator");
        assertEq(held - v.balanceOf(vault), bond, "held balance");
        assertEq(hv.bankFull().bondShares, 0, "released");
        vm.prank(curator);
        _ve().releaseBond();
        assertEq(v.balanceOf(curator), bond, "nothing more");
    }

    // ------------------------------------------------------------------ poke and processBreach

    /// @notice `processBreach` is open in every mode and processes a pending breach (its prologue's checkpoint).
    function test_U_mode__processBreachIsOpenInEveryMode() public {
        _driveBreaches(1);
        vm.prank(mallory);
        _ve().processBreach();
        assertEq(hv.bankFull().mode, Ladder.BREACHED, "processed: BREACHED");
        assertEq(_obs().pendingBreaches, 0, "acknowledged");
        vm.prank(mallory);
        _ve().processBreach();
        _driveBreaches(2);
        vm.prank(mallory);
        _ve().processBreach();
        assertEq(hv.bankFull().mode, Ladder.EXITED, "removed");
        vm.prank(mallory);
        _ve().processBreach();
    }

    /// @notice `poke` is open in every mode and rolls the witness once per epoch (`WitnessRolled` from the hook).
    function test_U_mode__pokeRollsTheWitnessInEveryMode() public {
        for (uint256 m; m < 3; ++m) {
            if (m == 1) _driveBreaches(1);
            if (m == 2) _retire();
            advance(tier.EPOCH);
            ITouchstoneHook(hook).refreshRef();
            vm.recordLogs();
            vm.prank(mallory);
            _ve().poke();
            Vm.Log[] memory logs = vm.getRecordedLogs();
            uint256 rolls;
            for (uint256 i; i < logs.length; ++i) {
                if (logs[i].emitter == hook && logs[i].topics[0] == ITouchstoneHook.WitnessRolled.selector) ++rolls;
            }
            assertEq(rolls, 1, "one roll per poke in a new epoch");
            assertEq(hv.bankFull().mode, m == 0 ? Ladder.NORMAL : m == 1 ? Ladder.BREACHED : Ladder.EXITED, "mode");
        }
    }

    /// @notice Pending removal (R2m-01): with the third breach latched but not processed, `topUpBond` and `retire`
    ///         revert `BadMode` after the prologue (the call reverts, so the breaches stay pending); `poke` then
    ///         reaches EXITED and `claim` stays open.
    function test_U_mode__pendingRemovalRevertsTopUpAndRetire() public {
        _fundUser(curator, 1e24);
        _deposit(curator, 1e21);
        _driveBreaches(3);
        vm.prank(curator);
        vm.expectRevert(Ladder.BadMode.selector);
        _ve().topUpBond(1e20);
        vm.prank(curator);
        vm.expectRevert(Ladder.BadMode.selector);
        _ve().retire();
        assertEq(_obs().pendingBreaches, 3, "still pending");
        _ve().poke();
        assertEq(hv.bankFull().mode, Ladder.EXITED, "the poke removes the curator");
        vm.prank(curator);
        _ve().claim();
    }
}
