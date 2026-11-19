// SPDX-License-Identifier: MIT
pragma solidity 0.8.26;

import {EpochRecord} from "../../src/types/EpochRecord.sol";

import {Vm} from "forge-std/Vm.sol";
import {TickMath} from "@uniswap/v4-core/src/libraries/TickMath.sol";
import {LiquidPoolBase} from "../utils/LiquidPoolBase.sol";
import {MockRefPool} from "../utils/MockRefPool.sol";
import {TouchstoneVault} from "../../src/TouchstoneVault.sol";
import {ExecLedger} from "../../src/libraries/ExecLedger.sol";
import {FullMath} from "@uniswap/v4-core/src/libraries/FullMath.sol";
import {Markout} from "../../src/types/Markout.sol";
import {Holdings} from "../../src/types/Holdings.sol";
import {Bank, Cohort, RING} from "../../src/types/Bank.sol";
import {SettleExtraOracle} from "../utils/SettleExtraOracle.sol";
import {IVaultEntries} from "../../src/interfaces/ITouchstoneVaultExtension.sol";
import {VaultCommon} from "../../src/TouchstoneVaultBase.sol";
import {Ladder} from "../../src/libraries/Ladder.sol";

/// @notice The markout settlement in the vault (prologue step 0 and `settle()`), on the real hook over the scripted
///         reference pool. A pending action is written through the harness seam (the actions that record one are built
///         later); its reserve is carved out of the bond with a non-bond part, as `Op.Charge` leaves it. Expectations
///         come from `ExecLedger.settleExtra` (checked bit for bit against the reference model's vectors) on inputs
///         the test computes itself: the window's tick from the scripted path, the supply and the holdings before
///         collect-first.
contract VaultSettleTest is LiquidPoolBase, SettleExtraOracle {
    event Settled(uint256 charge0, uint256 extra, int24 tickMk, bool fallbackUsed);

    int24 internal constant T_MK = 40; // the reference tick inside the markout window
    int24 internal constant T_AFTER = -100; // after the window
    uint128 internal constant RES_SHARES = 1e22;
    uint128 internal constant RES_BOND = 4e21;
    uint128 internal constant CHARGE0 = 4e15;
    uint128 internal constant RESERVE = 1e16;

    int24 internal lo;
    int24 internal hi;
    uint256 internal tA;
    Holdings internal pBefore;
    Holdings internal pAfter;

    function setUp() public override {
        super.setUp();
        (lo, hi) = (-600, 600);
        hv.placeForTest(lo, hi);
    }

    // ------------------------------------------------------------------ helpers

    /// @notice A pending action at the current block: the curator sold 1e16 token0 for nothing (a loss that grows with
    ///         the price, so each leg and the window's tick move the extra charge), with its reserve carved from the bond.
    function _pend() internal {
        tA = vm.getBlockTimestamp();
        Holdings memory a = hv.holdings();
        // a separate copy (a memory assignment would alias `a`)
        Holdings memory b = Holdings(a.idle0 + 1e16, a.idle1, a.L, a.lo, a.hi);
        (pBefore, pAfter) = (b, a);
        Bank memory bk = hv.bankFull();
        bk.bondShares -= RES_SHARES;
        (bk.reserveShares, bk.reserveBond, bk.reserveForfeit) = (RES_SHARES, RES_BOND, false);
        hv.setBank(bk);
        hv.setPending(uint40(tA), b, a, CHARGE0, RESERVE);
        // the record a real action leaves: the immediate charge burned at the action's price, the supply before it
        uint160 sp = hv.markout().s0; // setPending's default: the current spot
        uint256 S = hv.totalSupply();
        (uint256 navA,) = ExecLedger.value(a, sp);
        uint256 burn0 = FullMath.mulDivRoundingUp(CHARGE0, S, navA);
        hv.setMarkout(Markout(CHARGE0, RESERVE, sp, sp, uint128(burn0), uint128(S + burn0)));
    }

    /// @notice The scripted reference: `T_MK` over the window (tA + bt, tA + bt + H], `T_AFTER` after it; rolls to
    ///         the first block at which the settlement is due (tA + 2·bt + H).
    function _scriptWindowAndRollToDue(int24 tMk) internal {
        advance(1); // ts = tA + bt: the window opens after this second
        refPool.setTick(tMk);
        advance(tier.H_MARKOUT / blocktime); // ts = tA + bt + H: the window's last second
        refPool.setTick(T_AFTER);
        advance(1); // ts = tA + 2·bt + H: due
        assertEq(vm.getBlockTimestamp(), tA + 2 * blocktime + tier.H_MARKOUT, "due time");
    }

    function _expected(int24 tMk) internal returns (uint256 extra, uint256 burnS) {
        (extra,, burnS) = _settleExtra(pBefore, pAfter, hv.markout(), tMk, tier.FEE_TICKS, hv.feePips(), RES_SHARES);
    }

    function _tAction() internal view returns (uint40 t) {
        (t,,,,) = hv.pendingAction();
    }

    // ------------------------------------------------------------------ U-settle

    /// @notice Settled at the first due block on the delayed window's tick: the extra charge net of the immediate one,
    ///         re-priced at the markout leg (net of the immediate burn, on the record's supply); the bond part returns to the bond,
    ///         the rest of the non-bond part enters the current epoch's cohort; the record clears.
    function test_U_settle__settlesTheDelayedWindowAtTheFirstDueBlock() public {
        _pend();
        _scriptWindowAndRollToDue(T_MK);
        // fees the position earns before the settlement: collect-first runs after it and must not change its inputs
        _swap(true, 1e17, 50);
        _swap(false, 1e17, 50);
        (uint256 extra, uint256 burnS) = _expected(T_MK);
        assertGt(extra, 0, "an extra charge");
        assertLt(extra, RESERVE, "below the reserve cap");
        assertLt(burnS, RES_SHARES - RES_BOND, "within the non-bond part");
        uint256 S = hv.totalSupply();
        uint128 bond = hv.bankFull().bondShares;
        vm.expectEmit(address(hv));
        emit Settled(CHARGE0, extra, T_MK, false);
        hv.settle();
        assertEq(hv.totalSupply(), S - burnS, "burned exactly burnS");
        Bank memory b = hv.bankFull();
        assertEq(b.reserveShares, 0, "reserve cleared");
        assertEq(b.bondShares, bond + RES_BOND, "the bond part returns");
        uint32 e = uint32(vm.getBlockNumber() / tier.EPOCH);
        Cohort memory c = b.cohorts[e % RING];
        assertEq(c.epoch, e, "current epoch's cohort");
        assertEq(c.shares, RES_SHARES - RES_BOND - burnS, "the non-bond remainder");
        assertEq(_tAction(), 0, "record cleared");
    }

    /// @notice One block before the window has closed nothing settles, and the record stays.
    function test_U_settle__notDueBeforeTheWindowCloses() public {
        _pend();
        advance(1);
        refPool.setTick(T_MK);
        advance(tier.H_MARKOUT / blocktime); // ts = tA + bt + H: one block short
        hv.settle();
        assertEq(_tAction(), tA, "still pending");
        assertEq(hv.bankFull().reserveShares, RES_SHARES, "reserve intact");
    }

    /// @notice The window starts one block after the action: a different reference tick in the action's own block
    ///         (here the seed tick 0 up to tA + bt) does not enter the read.
    function test_U_settle__actionBlockExcludedFromTheWindow() public {
        _pend();
        refPool.setTick(-3000); // the action block's closing tick, set by ordering control
        _scriptWindowAndRollToDue(T_MK);
        (uint256 extra,) = _expected(T_MK);
        vm.expectEmit(address(hv));
        emit Settled(CHARGE0, extra, T_MK, false);
        hv.settle();
    }

    /// @notice A failed window read burns the whole reserve (the non-bond part first, then the bond part).
    function test_U_settle__failedReadBurnsTheWholeReserve() public {
        _pend();
        _scriptWindowAndRollToDue(T_MK);
        refPool.setMode(MockRefPool.Mode.Revert);
        uint256 S = hv.totalSupply();
        uint128 bond = hv.bankFull().bondShares;
        vm.expectEmit(address(hv));
        emit Settled(CHARGE0, RESERVE, 0, true);
        hv.settle();
        assertEq(hv.totalSupply(), S - RES_SHARES, "the whole reserve burned");
        assertEq(hv.bankFull().bondShares, bond, "nothing returns to the bond");
        assertEq(hv.bankFull().reserveShares, 0, "reserve cleared");
    }

    /// @notice Withdrawals never revert because of a settlement: with the window unreadable the withdrawal settles by
    ///         the fallback and pays out; under every failure mode of the reference.
    function test_U_settle__withdrawSucceedsWithTheWindowUnreadable() public {
        for (uint8 m = 1; m <= 5; ++m) {
            uint256 snap = vm.snapshotState();
            _pend();
            _scriptWindowAndRollToDue(T_MK);
            refPool.setMode(MockRefPool.Mode(m));
            uint256 S = hv.totalSupply();
            vm.prank(alice);
            (uint256 a0, uint256 a1) = IVaultEntries(address(hv)).withdraw(1e21, 0, 0, alice);
            assertGt(a0 + a1, 0, "paid out");
            assertEq(hv.totalSupply(), S - RES_SHARES - 1e21, "fallback burn, then the withdrawal");
            assertEq(_tAction(), 0, "settled");
            vm.revertToState(snap);
        }
    }

    /// @notice Deposits revert `MarkoutPending` until the settlement is due; a due deposit settles first, then mints.
    function test_U_settle__depositsBlockedWhilePendingThenSettleFirst() public {
        _pend();
        vm.prank(bob);
        vm.expectRevert(VaultCommon.MarkoutPending.selector);
        IVaultEntries(address(hv)).deposit(1e18, type(uint256).max, type(uint256).max, bob);
        _scriptWindowAndRollToDue(T_MK);
        (, uint256 burnS) = _expected(T_MK);
        uint256 S = hv.totalSupply();
        vm.prank(bob);
        IVaultEntries(address(hv)).deposit(1e18, type(uint256).max, type(uint256).max, bob);
        assertEq(hv.totalSupply(), S - burnS + 1e18, "settled, then minted");
        assertEq(_tAction(), 0, "settled");
    }

    /// @notice A breach processed while the action is pending leaves the reserve encumbered (`reserveForfeit`); at
    ///         settlement the non-bond remainder goes to the forfeit drip and the bond part back to the bond.
    function test_U_settle__breachLeavesTheReserveToFollowAtSettlement() public {
        refPool.setTick(hi + 1000);
        _driveBreaches(1);
        refPool.setTick(0);
        _pend();
        vm.prank(alice);
        IVaultEntries(address(hv)).withdraw(1e18, 0, 0, alice); // the checkpoint processes the breach; the settlement is not due
        Bank memory b = hv.bankFull();
        assertEq(b.breaches, 1, "breach processed");
        assertEq(b.reserveShares, RES_SHARES, "reserve kept");
        assertTrue(b.reserveForfeit, "marked to follow");
        uint128 pool = b.forfeitPool;
        uint128 bond = b.bondShares;
        _scriptWindowAndRollToDue(T_MK);
        (, uint256 burnS) = _expected(T_MK);
        hv.settle();
        b = hv.bankFull();
        assertEq(b.bondShares, bond + RES_BOND, "the bond part returns");
        assertEq(b.forfeitPool, pool + (RES_SHARES - RES_BOND - burnS), "the non-bond remainder to the drip");
        assertEq(b.reserveShares, 0, "cleared");
        // the drip restarts at the settlement's block and burns the whole pool over dripBlocks (blocks, not seconds)
        assertEq(b.forfeitStart, vm.getBlockNumber(), "the drip restarts now, in blocks");
        uint256 S = hv.totalSupply();
        uint128 owed = b.forfeitPool;
        _keepAlive(uint256(b.dripBlocks) + 10);
        hv.settle(); // a checkpoint drips
        assertEq(hv.bankFull().forfeitPool, 0, "dripped out");
        assertEq(hv.totalSupply(), S - owed, "burned to every holder");
    }

    /// @notice F-039 (MK-4): `retire` with a reserve pending leaves the reserve in place, marked to follow; at
    ///         settlement the bond part returns to the bond and the non-bond remainder joins the forfeiture drip.
    function test_U_settle__retireLeavesTheReserveToFollowAtSettlement() public {
        _pend();
        vm.prank(curator);
        IVaultEntries(address(hv)).retire();
        Bank memory b = hv.bankFull();
        assertEq(b.mode, Ladder.EXITED, "retired");
        assertEq(b.reserveShares, RES_SHARES, "reserve kept");
        assertTrue(b.reserveForfeit, "marked to follow");
        assertEq(_tAction(), tA, "the record stays pending");
        uint128 pool = b.forfeitPool;
        uint128 bond = b.bondShares;
        _scriptWindowAndRollToDue(T_MK);
        (, uint256 burnS) = _expected(T_MK);
        assertLt(burnS, RES_SHARES - RES_BOND, "precondition: a non-bond remainder");
        hv.settle();
        b = hv.bankFull();
        assertEq(b.reserveShares, 0, "cleared");
        assertEq(b.bondShares, bond + RES_BOND, "the bond part returns");
        assertGe(b.forfeitPool, pool, "the drip does not shrink at the settlement");
        assertEq(b.forfeitPool - pool, RES_SHARES - RES_BOND - burnS, "the non-bond remainder to the drip");
    }

    /// @notice F-039: `releaseBond` with a reserve pending settles it first (step 0), so the release pays the bond
    ///         including the reserve's returned bond part. (A release before the window closes cannot occur: the release
    ///         delay, V epochs, is far longer than the markout window.)
    function test_U_settle__releaseBondSettlesAPendingReserveFirst() public {
        _pend();
        vm.prank(curator);
        IVaultEntries(address(hv)).retire();
        Bank memory b = hv.bankFull();
        uint256 due = uint256(b.exitBlock) + b.releaseDelay;
        _scriptWindowAndRollToDue(T_MK);
        (, uint256 burnS) = _expected(T_MK);
        if (vm.getBlockNumber() < due) advance(due - vm.getBlockNumber());
        assertEq(_tAction(), tA, "precondition: still pending at the release");
        uint128 bond = hv.bankFull().bondShares;
        uint256 c0 = hv.balanceOf(curator);
        vm.prank(curator);
        IVaultEntries(address(hv)).releaseBond();
        b = hv.bankFull();
        assertEq(_tAction(), 0, "settled first");
        assertEq(b.reserveShares, 0, "reserve cleared");
        assertEq(hv.balanceOf(curator) - c0, uint256(bond) + RES_BOND, "the release includes the returned bond part");
        assertEq(b.bondShares, 0, "bond released");
        assertGt(burnS, 0, "the settlement burned the non-bond part first");
    }

    /// @notice P5-Ar-01 (spec §5.1 MK-4, disclosed): the settlement burns `burnS` computed on the action's recorded
    ///         supply, so a drip burn inside the markout window makes the burn larger than the value-exact one on the
    ///         supply at settlement. The extra falls on the curator's reserve; the holders gain it. Control: the same
    ///         window with no forfeit pool burns the same `burnS`, and there the recorded and the settlement supply agree.
    function test_U_settle__inWindowDripBurnOverBurnsTheCuratorsReserve() public {
        uint256[2] memory sSettle;
        uint256[2] memory burnS;
        uint256 sRec;
        uint256 snap = vm.snapshotState();
        for (uint256 drip; drip < 2; ++drip) {
            vm.revertToState(snap);
            _pend();
            sRec = hv.totalSupply();
            if (drip == 1) {
                Bank memory bk = hv.bankFull();
                bk.bondShares -= 1e21;
                (bk.forfeitPool, bk.forfeitTotal, bk.forfeitStart) = (1e21, 1e21, uint40(vm.getBlockNumber()));
                hv.setBank(bk);
            }
            advance(1);
            refPool.setTick(T_MK);
            advance(tier.H_MARKOUT / blocktime / 2);
            hv.settle(); // not due: a checkpoint inside the window drips (when a forfeit pool exists)
            advance(tier.H_MARKOUT / blocktime - tier.H_MARKOUT / blocktime / 2);
            refPool.setTick(T_AFTER);
            advance(1);
            (, burnS[drip]) = _expected(T_MK);
            sSettle[drip] = hv.totalSupply();
            hv.settle();
            Bank memory b = hv.bankFull();
            uint32 e = uint32(vm.getBlockNumber() / tier.EPOCH);
            assertEq(b.reserveShares, 0, "settled");
            // the reserve burned exactly burnS, computed on the recorded supply: the remainder enters the cohort
            assertEq(b.cohorts[e % RING].shares, RES_SHARES - RES_BOND - burnS[drip], "the reserve burned burnS");
        }
        assertEq(burnS[1], burnS[0], "the burn ignores the in-window drip");
        assertEq(sSettle[0], sRec, "control: no in-window burn, the settlement supply is the recorded one");
        assertLt(sSettle[1], sRec, "the drip burned inside the window");
        // the value-exact burn on the settlement supply is burnS·S_settle/S_record: the difference is the over-burn,
        // taken from the curator's reserve
        uint256 exact = FullMath.mulDiv(burnS[1], sSettle[1], sRec);
        assertGt(burnS[1], exact, "over-burn on the curator's reserve");
    }

    /// @notice The record of a settlement (spec §3.9, S19D-03): its extra charge, its burn split from the op's returns
    ///         (`bankBurned += nb − relNb`, `bondBurned += reserveBond − relBond`) and, for a reserve forfeited by a
    ///         breach while pending, `forfeited += relNb` (the non-bond remainder joining the drip), all in the record
    ///         of the stored epoch.
    function test_U_record__settlementRecordsItsBurnsAndAForfeitedReserve() public {
        refPool.setTick(hi + 1000);
        _driveBreaches(1);
        refPool.setTick(0);
        _pend();
        vm.prank(alice);
        IVaultEntries(address(hv)).withdraw(1e18, 0, 0, alice); // processes the breach: the reserve is marked
        assertTrue(hv.bankFull().reserveForfeit, "precondition: marked to follow");
        _scriptWindowAndRollToDue(T_MK);
        (uint256 extra, uint256 burnS) = _expected(T_MK);
        assertGt(burnS, 0, "precondition: a burn");
        assertLt(burnS, RES_SHARES - RES_BOND, "precondition: a non-bond remainder");
        uint32 e = hv.scalars().epoch;
        EpochRecord memory b0 = hv.record(e);
        hv.settle();
        EpochRecord memory r = hv.record(e);
        assertEq(r.charge - b0.charge, extra, "the settlement's extra");
        assertEq(r.bankBurned - b0.bankBurned, burnS, "the non-bond part burned first");
        assertEq(r.bondBurned, b0.bondBurned, "nothing from the bond's part");
        assertEq(r.forfeited - b0.forfeited, RES_SHARES - RES_BOND - burnS, "the forfeited reserve's remainder");
    }

    /// @notice The released non-bond reserve enters the settlement's epoch, not the last checkpoint's: the action sits
    ///         at the end of an epoch and the settlement in the next one; an occupied slot of an older epoch moves its
    ///         matured occupant to `maturedPending`.
    function test_U_settle__releaseEntersTheCurrentEpochsCohort() public {
        uint256 ep = tier.EPOCH;
        _keepAlive(RING * ep); // past the first RING epochs, so an older epoch maps to every slot
        advance(ep - vm.getBlockNumber() % ep - 3); // three blocks before an epoch boundary
        hv.settle(); // a checkpoint in the old epoch
        uint32 e0 = uint32(vm.getBlockNumber() / ep);
        _pend();
        Bank memory bk = hv.bankFull();
        uint32 e1 = e0 + 1;
        bk.cohorts[e1 % RING] = Cohort(e1 - uint32(RING), 7e20); // an older epoch's occupant of e1's slot
        bk.bondShares -= 7e20;
        hv.setBank(bk);
        _scriptWindowAndRollToDue(T_MK);
        assertEq(vm.getBlockNumber() / ep, e1, "settles in the next epoch");
        (, uint256 burnS) = _expected(T_MK);
        hv.settle();
        Bank memory b = hv.bankFull();
        assertEq(b.cohorts[e1 % RING].epoch, e1, "the settlement's epoch");
        assertEq(b.cohorts[e1 % RING].shares, RES_SHARES - RES_BOND - burnS, "the non-bond remainder");
        // the occupant moved to maturedPending, which this call's checkpoint then vests (the gate is open here)
        assertEq(uint256(b.maturedPending) + b.vestedUnclaimed, 7e20, "the occupant left the slot as matured pay");
    }

    /// @notice Window means within FEE_TICKS of either tick bound settle (both legs clamped) and a withdrawal succeeds.
    function test_U_settle__windowAtTheTickBoundsSettles() public {
        int24[2] memory ts = [TickMath.MAX_TICK - 2, TickMath.MIN_TICK + 2];
        for (uint256 i; i < 2; ++i) {
            uint256 snap = vm.snapshotState();
            _pend();
            _scriptWindowAndRollToDue(ts[i]);
            (uint256 extra,) = _expected(ts[i]);
            vm.expectEmit(address(hv));
            emit Settled(CHARGE0, extra, ts[i], false);
            vm.prank(alice);
            IVaultEntries(address(hv)).withdraw(1e21, 0, 0, alice);
            assertEq(_tAction(), 0, "settled");
            vm.revertToState(snap);
        }
    }

    // ------------------------------------------------------------------ the gas guard

    /// @notice The guard: a due settlement with less than 2·REF_GAS_CAP left reverts `RefGas`.
    function test_U_settle__gasGuardRevertsBelowTwiceTheCap() public {
        _pend();
        _scriptWindowAndRollToDue(T_MK);
        vm.expectRevert(VaultCommon.RefGas.selector);
        hv.settle{gas: 2 * uint256(tier.REF_GAS_CAP)}();
        hv.settle{gas: 4 * uint256(tier.REF_GAS_CAP)}();
        assertEq(_tAction(), 0, "settles with enough gas");
    }

    /// @notice Across a sweep of gas limits on `settle()` no successful call takes the fallback while a full-gas read
    ///         succeeds. The reference here fails its read early (cheaply) when called with less than its full-read
    ///         gas: against a pool whose read only runs out of gas, a starved read leaves under 2·REF_GAS_CAP/63 gas (the
    ///         vault's own 1/64 at the hook call plus the hook's at the staticcall), too little for the settlement's
    ///         stores, the FeeBank call and the burn, so the call cannot complete into the fallback at all. Control: with the guard off some limit starves the
    ///         read into the fallback and the call succeeds.
    function test_U_settle__noGasLimitStarvesTheReadIntoTheFallback() public {
        uint256 snap = vm.snapshotState();
        (uint256 starved, uint256 settled) = _sweep(false);
        assertEq(starved, 0, "guarded: no starved fallback");
        assertGt(settled, 0, "guarded: some limit settles on the read");
        vm.revertToState(snap);
        (starved,) = _sweep(true);
        assertGt(starved, 0, "control: the guard removed finds a starving limit");
    }

    function _sweep(bool guardOff) internal returns (uint256 starved, uint256 settled) {
        _pend();
        _scriptWindowAndRollToDue(T_MK);
        // a read needs the cap less 20k to succeed (s19: with the record's stores after the read, a starved read at half
        // the cap no longer leaves the flow enough gas to complete, so the control needs a wider window; at the whole
        // cap the hook's capped forward can never meet the floor)
        refPool.setGasFloor(tier.REF_GAS_CAP - 20_000);
        hv.setGasGuardOff(guardOff);
        for (uint256 g = 100_000; g < 1_400_000; g += 5_000) {
            uint256 s = vm.snapshotState();
            vm.recordLogs();
            (bool success,) = address(hv).call{gas: g}(abi.encodeCall(TouchstoneVault.settle, ()));
            if (success) {
                if (_settledFallback()) ++starved;
                else ++settled;
            }
            vm.revertToState(s);
        }
    }

    function _settledFallback() internal returns (bool) {
        bytes32 sig = keccak256("Settled(uint256,uint256,int24,bool)");
        Vm.Log[] memory logs = vm.getRecordedLogs();
        for (uint256 i; i < logs.length; ++i) {
            if (logs[i].emitter == address(hv) && logs[i].topics[0] == sig) {
                (,,, bool fb) = abi.decode(logs[i].data, (uint256, uint256, int24, bool));
                return fb;
            }
        }
        return false;
    }

    function _two(uint32 a, uint32 b) internal pure returns (uint32[] memory s) {
        s = new uint32[](2);
        (s[0], s[1]) = (a, b);
    }
}
