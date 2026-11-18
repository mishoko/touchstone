// SPDX-License-Identifier: MIT
pragma solidity 0.8.26;

import {FullMath} from "@uniswap/v4-core/src/libraries/FullMath.sol";
import {StateLibrary} from "@uniswap/v4-core/src/libraries/StateLibrary.sol";
import {IPoolManager} from "@uniswap/v4-core/src/interfaces/IPoolManager.sol";
import {LiquidPoolBase} from "../utils/LiquidPoolBase.sol";
import {Scalars} from "../utils/VaultHarness.sol";
import {ITouchstoneHook} from "../../src/interfaces/ITouchstoneHook.sol";
import {IVaultEntries} from "../../src/interfaces/ITouchstoneVaultExtension.sol";
import {Obs} from "../../src/types/Obs.sol";
import {Bank, Cohort, RING, V} from "../../src/types/Bank.sol";
import {PreSwap} from "../../src/types/PreSwap.sol";
import {Ladder} from "../../src/libraries/Ladder.sol";
import {VaultCommon} from "../../src/TouchstoneVaultBase.sol";

/// @notice The breach ladder and the bank through the P4 entry points (spec §3.3, §3.7, §3.11, §4.5, §4.6), on the
///         factory-deployed harness with third-party liquidity around tick 0, a scripted reference pool and the TEST
///         tier (GRID 20, CURE_BLOCKS 200, EPOCH 120, RESET_RUN 2, V 4). Every placement whose base matters is a real
///         curator `setPositions` (`placeForTest` leaves δ₀ = 0).
contract VaultLadderTest is LiquidPoolBase {
    using StateLibrary for IPoolManager;

    uint256 internal constant WAD = 1e18;
    int24 internal constant LO = -1800;
    int24 internal constant HI = 200;

    function setUp() public override {
        super.setUp();
        _setRef(0);
    }

    // ------------------------------------------------------------------ helpers

    function _ve() internal view returns (IVaultEntries) {
        return IVaultEntries(vault);
    }

    /// @dev The reference pool's tick from now on (its TWAP follows over `REF_WINDOW`).
    function _setRef(int24 t) internal {
        refPool.setTick(t);
    }

    function _none() internal pure returns (PreSwap memory) {
        return PreSwap(false, false, 0);
    }

    /// @dev A real curator placement on [lo, hi) (δ₀ > 0, so base accrues), its markout settled; ends with a checkpoint.
    function _place(int24 lo, int24 hi) internal {
        hv.placeForTest(lo, hi);
        advance(tier.COOLDOWN);
        vm.prank(curator);
        hv.setPositions(lo, hi, _none(), 0, type(uint256).max);
        assertGt(hv.scalars().delta0, 0, "precondition: a real placement deploys");
        _keepAlive(tier.H_MARKOUT + 20);
        hv.settle();
    }

    /// @dev The base for `dtau` in-range blocks at the stored NAV₀ and δ₀ (spec §3.5).
    function _baseFor(uint64 dtau) internal view returns (uint256) {
        Scalars memory s = hv.scalars();
        return FullMath.mulDiv(FullMath.mulDiv(tier.m_perBlock, s.nav0, WAD), uint256(s.delta0) * dtau, WAD);
    }

    /// @dev Moves bond shares into an epoch-`e` cohort, maturedPending and vestedUnclaimed.
    function _plant(uint32 e, uint128 c, uint128 mp, uint128 vu) internal {
        Bank memory b = hv.bankFull();
        b.cohorts[e % RING] = Cohort(e, b.cohorts[e % RING].epoch == e ? b.cohorts[e % RING].shares + c : c);
        b.maturedPending += mp;
        b.vestedUnclaimed += vu;
        b.bondShares -= c + mp + vu;
        hv.setBank(b);
    }

    function _unvested() internal view returns (uint256 u) {
        Bank memory b = hv.bankFull();
        for (uint256 k; k < RING; ++k) u += b.cohorts[k].shares;
        u += b.maturedPending;
    }

    function _epochNow() internal view returns (uint32) {
        return uint32(block.number / tier.EPOCH);
    }

    /// @dev Keeps the reference in range until a reset ends the run (`RESET_RUN` consecutive in-range anchors).
    function _reset() internal {
        _setRef(0);
        for (uint256 i; _obs().outBlocks != 0 || _obs().resetStreak != 0 || i == 0; ++i) {
            require(i < 100, "no reset");
            _keepAlive(10);
        }
        assertEq(_obs().outBlocks, 0, "the run ended");
    }

    // ------------------------------------------------------------------ U-forfeit-quiet

    /// @notice No vault checkpoint for days (only the hook observes), then a breach: the unminted base up to the
    ///         breach and every not-yet-vested cohort are forfeited, the pay vested by an earlier checkpoint is kept.
    ///         Exact: the one checkpoint mints nothing, moves exactly the planted unvested buckets into the drip and
    ///         keeps `vestedUnclaimed`. Control (accrue-before-forfeit): the base that order would mint is > 0.
    function test_U_forfeit_quiet__noCheckpointForDaysThenABreach() public {
        _place(LO, HI);
        _plant(_epochNow(), 3e20, 1e20, 2e20);
        uint256 unvested = _unvested(); // the planted buckets and the base minted by the placement's settlement
        assertEq(hv.bankFull().forfeitPool, 0, "precondition: no drip running");
        Scalars memory s0 = hv.scalars();
        _keepAlive(400); // in range, observed; no vault checkpoint
        _setRef(5000);
        _driveBreaches(1);
        _keepAlive(60);
        Obs memory o = _obs();
        assertEq(o.pendingBreaches, 1, "one breach pending");
        assertEq(o.tauV, o.tauVAtLastPending, "no in-range time after the latch");
        uint256 forfeitable = _baseFor(o.tauVAtLastPending - s0.tauV0);
        assertGt(forfeitable, 0, "control: accrue-before-forfeit would mint this base");
        uint256 S = hv.totalSupply();
        _ve().processBreach();
        Bank memory b = hv.bankFull();
        assertEq(hv.totalSupply(), S, "nothing minted (the base up to b_last is forfeited)");
        assertEq(_unvested(), 0, "every unvested bucket forfeited");
        assertEq(b.forfeitPool, unvested, "exactly the unvested buckets in the drip");
        assertEq(b.vestedUnclaimed, 2e20, "vested pay kept");
        assertEq(b.breaches, 1, "one breach");
        assertEq(b.mode, Ladder.BREACHED, "BREACHED");
        assertEq(hv.scalars().baseOwed, 0, "nothing owed");
        assertEq(hv.scalars().tauV0, o.tauV, "the segment restarts at the checkpoint");
    }

    // ------------------------------------------------------------------ U-forfeit-2gap

    struct TwoGap {
        uint32 breaches;
        uint8 mode;
        uint256 unvested;
        uint256 vested;
        uint256 bond;
        uint256 inRangeBase;
        uint256 mintedAtEnd;
    }

    /// @dev Latch, reset, in-range days, latch, one checkpoint (`pokeBetween`: a poke after the in-range days).
    function _twoGap(uint32 startBreaches, bool pokeBetween) internal returns (TwoGap memory r) {
        _place(LO, HI);
        if (startBreaches > 0) {
            Bank memory bb = hv.bankFull();
            (bb.breaches, bb.mode) = (startBreaches, Ladder.BREACHED);
            hv.setBank(bb);
        }
        _plant(_epochNow(), 3e20, 0, 1e20);
        _setRef(5000);
        _driveBreaches(1);
        uint64 tauFirst = _obs().tauVAtLastPending;
        _reset();
        _keepAlive(400); // in-range days
        if (pokeBetween) _ve().poke();
        _setRef(5000);
        _driveBreaches(1);
        _keepAlive(40);
        Obs memory o = _obs();
        // the base earned between the two latches, which a cutoff at the first latch would keep
        r.inRangeBase = _baseFor(o.tauVAtLastPending - tauFirst);
        uint256 S = hv.totalSupply();
        _ve().processBreach();
        r.mintedAtEnd = hv.totalSupply() > S ? hv.totalSupply() - S : 0;
        Bank memory b = hv.bankFull();
        (r.breaches, r.mode, r.unvested, r.vested, r.bond) =
            (b.breaches, b.mode, _unvested(), b.vestedUnclaimed, b.bondShares);
    }

    /// @notice Two latches, one checkpoint: the base up to `b_last` (the in-range days included) is forfeited,
    ///         `breaches` 0 → 2 (BREACHED) and 2 → 4 (EXITED, §4.5); the curator's pay equals a run with a poke between
    ///         the latches. Control (first-cutoff-only): the in-range days earned base (> 0) that a cutoff at the first
    ///         latch would have minted.
    function test_U_forfeit_2gap__twoLatchesOneCheckpoint() public {
        for (uint32 start; start <= 2; start += 2) {
            uint256 snap = vm.snapshotState();
            TwoGap memory a = _twoGap(start, false);
            vm.revertToState(snap);
            TwoGap memory b = _twoGap(start, true);
            vm.revertToState(snap);
            assertGt(a.inRangeBase, 0, "control: the in-range days earned base a first cutoff would keep");
            assertEq(a.mintedAtEnd, 0, "the one checkpoint mints nothing: the base up to b_last is forfeited");
            assertEq(a.breaches, start + 2, "both latches counted");
            assertEq(a.mode, start == 0 ? Ladder.BREACHED : Ladder.EXITED, "the mode per section 4.5");
            assertEq(a.unvested, 0, "every unvested bucket forfeited");
            assertEq(a.vested, 1e20, "vested pay kept");
            // equal to the run with a poke between the latches (the curator's pay and the ladder)
            assertEq(a.breaches, b.breaches, "breaches equal");
            assertEq(a.mode, b.mode, "mode equal");
            assertEq(a.unvested, b.unvested, "unvested equal");
            assertEq(a.vested, b.vested, "vested equal");
            assertEq(a.bond, b.bond, "bond equal");
        }
    }

    // ------------------------------------------------------------------ U-vest-park

    /// @notice The curator parks (stays silent past maturity), re-places, pokes and claims: no pay vests before the
    ///         verified return (the registration), not at a poke after the gap nor after one in-range anchor; the pay
    ///         in `maturedPending` vests at the first poke after the re-placement and `claim` pays exactly it.
    function test_U_vest_park__noVestBeforeTheVerifiedReturn() public {
        _place(LO, HI);
        _keepAlive(200);
        _ve().poke(); // mints the base into this epoch's cohort
        _plant(_epochNow(), 1e21, 0, 0); // and more pay in it (the re-placement's reserve is taken from unvested pay)
        uint256 cohort = _unvested();
        assertGt(cohort, 0, "precondition: a cohort");
        // parks: the reference leaves the range (spot follows it), then silence past maturity
        _outAbove(230);
        advance((V + 2) * tier.EPOCH);
        _ve().poke();
        Bank memory b = hv.bankFull();
        assertTrue(_obs().gapSinceVerify, "the silence left a gap");
        assertEq(b.vestedUnclaimed, 0, "nothing vests at the gap");
        assertEq(b.maturedPending, cohort, "the matured cohort waits in maturedPending");
        // observed anchors while parked are no verified return (out of range: no reset run, no vest)
        for (uint256 i; i < 3; ++i) {
            advance(tier.GRID);
            ITouchstoneHook(hook).refreshRef();
            _ve().poke();
        }
        assertTrue(_obs().gapSinceVerify, "still unverified");
        assertEq(hv.bankFull().vestedUnclaimed, 0, "nothing vests while parked");
        assertEq(_obs().pendingBreaches + hv.bankFull().breaches, 0, "precondition: no breach yet");
        // the verified return: a real re-placement around spot and the reference (its charge may burn from
        // maturedPending)
        vm.prank(curator);
        hv.setPositions(-1760, 240, _none(), 0, type(uint256).max);
        assertFalse(_obs().gapSinceVerify, "the registration clears the gap");
        uint256 mp = hv.bankFull().maturedPending;
        assertGt(mp, 0, "maturedPending survives the charge");
        assertEq(hv.bankFull().vestedUnclaimed, 0, "nothing vested by the placement itself");
        advance(1);
        _ve().poke();
        assertEq(hv.bankFull().vestedUnclaimed, mp, "maturedPending vests after the verified return");
        vm.prank(curator);
        _ve().claim();
        assertEq(hv.balanceOf(curator), mp, "claimed exactly");
    }

    // ------------------------------------------------------------------ U-gap

    function _wit() internal view returns (int24 lo, int24 hi) {
        uint256 s1 = uint256(vm.load(hook, bytes32(uint256(1))));
        lo = int24(uint24(s1 >> 192));
        hi = int24(uint24(s1 >> 216));
    }

    /// @notice A gap of V+3 epochs, and one of 9 grid boundaries: the poke after it costs a bounded amount of gas
    ///         (under G-3's 700k budget) and rolls the witness centred on `epochStartRef` (exact range, liquidity on
    ///         the epoch's salt). The record clause (at most two records) comes with the records (s19).
    function test_U_gap__aLongGapThenOnePoke() public {
        hv.placeForTest(-600, 600);
        uint256[2] memory gaps = [uint256((V + 3) * tier.EPOCH), 9 * tier.GRID + 7];
        for (uint256 i; i < 2; ++i) {
            advance(gaps[i]);
            uint256 g = gasleft();
            _ve().poke();
            uint256 used = g - gasleft();
            assertLe(used, 700_000, "bounded gas");
            int24 c = ITouchstoneHook(hook).epochStartRef();
            int24 sp = TICK_SPACING;
            int24 base = c / sp * sp;
            if (c < 0 && c % sp != 0) base -= sp;
            (int24 lo, int24 hi) = _wit();
            assertEq(lo, base - tier.W_TICKS / 2, "witness lo centred on epochStartRef");
            assertEq(hi, base + tier.W_TICKS / 2, "witness hi");
            (uint128 liq,,) = IPoolManager(address(manager)).getPositionInfo(
                poolId, hook, lo, hi, keccak256(abi.encode("witness", _obs().epoch))
            );
            assertGt(liq, 0, "the epoch's witness is live");
        }
    }

    // ------------------------------------------------------------------ U-breach-dodge, U-breach-capdodge

    /// @dev The reference leaves the placed range above (hi = 200) and spot follows it (within the fence).
    function _outAbove(int24 ref) internal {
        _setRef(ref);
        _keepAlive(60); // the TWAP reaches ref
        _swap(false, 1e30, ref - _tick());
        assertEq(_tick(), ref - 1 >= _tick() ? _tick() : _tick(), "");
    }

    /// @notice A placement that does not contain spot (or the reference) and a dust placement (δ < DELTA_MIN) both
    ///         revert, so the clock keeps counting and the breach latches on time. Control: a registration without
    ///         the δ and containment rules (the harness's seam) restarts the run, and the breach does not latch then.
    function test_U_breach_dodge__dustAndOffSpotPlacementsCannotResetTheClock() public {
        _place(LO, HI);
        _outAbove(230);
        token0.approve(address(hv), type(uint256).max);
        hv.creditIdle(1e26, 0); // idle overwhelmingly token0: any W-wide range containing spot is dust
        uint32 seq = _obs().breachSeq;
        uint256 snap = vm.snapshotState();
        uint256 n;
        while (_obs().breachSeq == seq) {
            require(++n < 40, "no breach");
            advance(tier.COOLDOWN);
            ITouchstoneHook(hook).refreshRef();
            vm.startPrank(curator);
            vm.expectRevert(VaultCommon.NotContained.selector);
            hv.setPositions(400, 2400, _none(), 0, type(uint256).max); // contains neither spot nor the reference
            vm.expectRevert(VaultCommon.DeltaMin.selector);
            hv.setPositions(-1760, 240, _none(), 0, type(uint256).max); // contains both: dust
            vm.stopPrank();
        }
        uint256 latchedAt = block.number;
        vm.revertToState(snap);
        // control: the same schedule with a rule-free registration each time restarts the run
        while (block.number < latchedAt) {
            advance(tier.COOLDOWN);
            ITouchstoneHook(hook).refreshRef();
            hv.placeForTest(-1760, 240);
        }
        assertEq(_obs().breachSeq, seq, "control: without the rules the clock resets and no breach latches");
    }

    /// @notice Dust swaps (`rebalanceSwap`, the action that never registers) cannot pause the clock: the breach
    ///         latches within CURE_BLOCKS of observed out-of-range time. Control: an action that paused the clock (a
    ///         run restarted at each action, the seam) stalls it.
    function test_U_breach_capdodge__dustSwapsCannotPauseTheClock() public {
        _place(LO, HI);
        _outAbove(230);
        uint32 seq = _obs().breachSeq;
        uint256 snap = vm.snapshotState();
        uint256 n;
        while (_obs().breachSeq == seq) {
            require(++n < 40, "no breach");
            advance(tier.COOLDOWN);
            ITouchstoneHook(hook).refreshRef();
            vm.prank(curator);
            hv.rebalanceSwap(true, 1e6, type(uint256).max);
        }
        assertGe(n, 2, "dust swaps ran while the clock counted");
        uint256 latchedAt = block.number;
        vm.revertToState(snap);
        while (block.number < latchedAt) {
            advance(tier.COOLDOWN);
            ITouchstoneHook(hook).refreshRef();
            vm.prank(curator);
            hv.rebalanceSwap(true, 1e6, type(uint256).max);
            hv.placeForTest(LO, HI); // the action-capped pause: the run restarts at each action
        }
        assertEq(_obs().breachSeq, seq, "control: a clock paused by actions stalls");
    }

    // ------------------------------------------------------------------ U-ladder-stall

    /// @notice Re-place-then-abandon, then wrong-side dust: breaches 2 and 3 latch. The re-placement starts a new run
    ///         (breach 2 needs a full CURE_BLOCKS in it); the wrong-side placement reverts, so the third breach
    ///         latches in the same run as the second, from the every-CURE rule. Control (run-not-restarted): that
    ///         run's `outBlocks` reach 2·CURE_BLOCKS, which a clock latching once per run would stall at.
    function test_U_ladder_stall__replaceThenAbandonAndWrongSideDust() public {
        _place(LO, HI);
        _outAbove(230);
        _driveBreaches(1); // breach 1 (run 1)
        // re-place on the token1 side around spot and the reference: a new run
        advance(tier.COOLDOWN);
        vm.prank(curator);
        hv.setPositions(-1760, 240, _none(), 0, type(uint256).max);
        assertEq(_obs().outBlocks, 0, "a registration starts a new run");
        // abandon: the reference leaves the new range (spot follows), then wrong-side dust each cooldown
        _outAbove(290);
        uint32 seq0 = _obs().breachSeq;
        uint256 n;
        while (_obs().breachSeq < seq0 + 2) {
            require(++n < 80, "no breaches");
            advance(tier.COOLDOWN);
            ITouchstoneHook(hook).refreshRef();
            // after the third latch an action's prologue would remove the curator (BadMode): stop at the latch
            if (_obs().breachSeq >= seq0 + 2) break;
            vm.prank(curator);
            vm.expectRevert(VaultCommon.NotContained.selector);
            hv.setPositions(-2000, 0, _none(), 0, type(uint256).max); // the wrong side: contains neither
        }
        assertGe(_obs().outBlocks, 2 * uint256(tier.CURE_BLOCKS), "control: the third breach came from the same run");
        _ve().poke();
        Bank memory b = hv.bankFull();
        assertEq(b.breaches, 3, "breaches 2 and 3 latched");
        assertEq(b.mode, Ladder.EXITED, "removed");
    }
}
