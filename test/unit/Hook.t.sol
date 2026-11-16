// SPDX-License-Identifier: MIT
pragma solidity 0.8.26;

import {Vm} from "forge-std/Test.sol";
import {TickMath} from "@uniswap/v4-core/src/libraries/TickMath.sol";
import {ITouchstoneHook} from "../../src/interfaces/ITouchstoneHook.sol";
import {TouchstoneHook} from "../../src/TouchstoneHook.sol";
import {HookArgs} from "../../src/interfaces/ITouchstoneFactory.sol";
import {MedianOracle} from "../../src/libraries/MedianOracle.sol";
import {Obs} from "../../src/types/Obs.sol";
import {Tier} from "../../src/types/Tier.sol";
import {HookBase} from "../utils/HookBase.sol";
import {HookHarness} from "../utils/HookHarness.sol";
import {MockRefPool} from "../utils/MockRefPool.sol";
import {Create2Deployer} from "../utils/Create2Deployer.sol";

/// @notice Hook unit tests at the TEST tier (12 s blocks, GRID 20 blocks = 240 s, CURE_BLOCKS 200, RESET_RUN 2).
///         The pool price is scripted through the harness; the reference is a scripted MockRefPool.
contract HookTest is HookBase {
    uint256 internal constant G = 20; // TEST GRID
    uint256 internal constant CURE = 200;

    function setUp() public {
        _setUpHook(false);
        refConst(0);
    }

    // ================================================================== U-1: the snapshot ring (records)

    function test_U_1__oneEntryPerBlockAtTheFirstTouch() public {
        pool(5);
        snap();
        pool(7); // a later touch in the same block records nothing
        snap();
        (uint8 head, uint8 count) = h.ringHeadCount();
        assertEq(count, 1);
        assertEq(head, 1);
        assertEq(h.ringAt(0), MedianOracle.pack(vm.getBlockNumber(), 5));
        advance(1);
        snap();
        (, count) = h.ringHeadCount();
        assertEq(count, 2);
        assertEq(h.ringAt(1), MedianOracle.pack(vm.getBlockNumber(), 7));
        // before arming a snapshot credits nothing
        Obs memory o = h.obs();
        assertEq(o.tauV + o.tauB + o.outBlocks, 0);
        (, uint40 lastSnap,,,,) = h.state();
        assertEq(lastSnap, vm.getBlockNumber());
    }

    function test_U_1__ringWrapsAt32AndMed9IsTheLowerMedianOfTheLast9() public {
        int24[40] memory ticks;
        for (uint256 i; i < 40; ++i) {
            ticks[i] = int24(int256((i * 7919) % 101)) - 50;
            pool(ticks[i]);
            snap();
            if (i == 3) {
                // four entries: the lower median
                int24[] memory a = new int24[](4);
                for (uint256 x; x < 4; ++x) {
                    a[x] = ticks[x];
                }
                assertEq(h.obs().med9, _sortedAt(a, 1));
            }
            advance(1);
        }
        (uint8 head, uint8 count) = h.ringHeadCount();
        assertEq(count, 32);
        assertEq(head, 40 % 32);
        for (uint256 i = 8; i < 40; ++i) {
            assertEq(MedianOracle.tickOf(h.ringAt(i % 32)), ticks[i]);
            assertEq(h.ringAt(i % 32) >> 24, vm.getBlockNumber() - 40 + i);
        }
        int24[] memory last9 = new int24[](9);
        for (uint256 x; x < 9; ++x) {
            last9[x] = ticks[31 + x];
        }
        assertEq(h.obs().med9, _sortedAt(last9, 4));
    }

    // ================================================================== U-clock

    /// An unplaced (EMPTY) vault observed out of range latches at every CURE_BLOCKS and reaches three breaches.
    function test_U_clock__emptyVaultLatchesEveryCureBlocksAndReachesThree() public {
        arm();
        uint256 b0 = vm.getBlockNumber();
        for (uint256 i; i < 30; ++i) {
            advance(G);
            touch();
            if (i == 9) {
                Obs memory o1 = h.obs();
                assertEq(o1.breachSeq, 1);
                assertEq(o1.lastPendingBlock, b0 + CURE);
            }
        }
        Obs memory o = h.obs();
        assertTrue(o.emptyRange);
        assertEq(o.outBlocks, 600);
        assertEq(o.breachSeq, 3);
        assertEq(o.pendingBreaches, 3);
        assertEq(o.lastPendingBlock, b0 + 3 * CURE);
        // acknowledging subtracts the processed count and never clears more
        ack(2);
        assertEq(h.obs().pendingBreaches, 1);
        ack(5);
        assertEq(h.obs().pendingBreaches, 0);
        assertEq(h.obs().breachSeq, 3);
    }

    /// One span crossing several multiples of CURE_BLOCKS latches each crossing at its own block.
    function test_U_clock__oneLatchPerCureBlocksCrossedAtTheCrossingBlock() public {
        arm();
        for (uint256 i; i < 9; ++i) {
            advance(G);
            touch();
        }
        assertEq(h.obs().outBlocks, 180);
        uint256 last = vm.getBlockNumber();
        vm.recordLogs();
        advance(G); // 180 -> 200: one crossing, at last + 20
        touch();
        Obs memory o = h.obs();
        assertEq(o.breachSeq, 1);
        assertEq(o.lastPendingBlock, last + 20);
        assertEq(o.tauVAtLastPending, o.tauV);
        assertEq(_count(vm.getRecordedLogs(), ITouchstoneHook.BreachLatched.selector), 1);
    }

    function test_U_clock__registrationStartsARunAndKeepsPendingBreaches() public {
        arm();
        for (uint256 i; i < 10; ++i) {
            advance(G);
            touch();
        }
        assertEq(h.obs().pendingBreaches, 1);
        advance(3 * G); // a silence: the gap is set
        touch();
        assertTrue(h.obs().gapSinceVerify);
        advance(3);
        vm.recordLogs();
        register(LO, HI, 7);
        Obs memory o = h.obs();
        assertFalse(o.emptyRange);
        assertEq(o.outBlocks, 0);
        assertEq(o.resetStreak, 0);
        assertFalse(o.gapSinceVerify);
        assertEq(o.pendingBreaches, 1);
        assertEq(o.breachSeq, 1);
        assertEq(o.refTick, 7);
        assertEq(o.refCachedAt, vm.getBlockNumber());
        (uint32 refGrid,,, int24 lo, int24 hi,) = h.state();
        assertEq(refGrid, vm.getBlockTimestamp() / tier.GRID_SEC);
        assertEq(lo, LO);
        assertEq(hi, HI);
        Vm.Log[] memory l = vm.getRecordedLogs();
        assertEq(_count(l, ITouchstoneHook.GapCleared.selector), 1);
        assertEq(_count(l, ITouchstoneHook.RangeRegistered.selector), 1);
    }

    /// A gap is cleared, and the run ended, only after RESET_RUN consecutive in-range anchors.
    function test_U_clock__resetAfterResetRunConsecutiveInRangeAnchors() public {
        arm();
        register(LO, HI, 0);
        refConst(5000);
        advance(G);
        touch(); // cache out of range
        advance(G);
        touch();
        advance(100); // silence: the gap is set
        refConst(0);
        touch(); // first in-range anchor
        Obs memory o = h.obs();
        assertTrue(o.gapSinceVerify);
        assertEq(o.resetStreak, 1);
        assertGt(o.outBlocks, 0);
        advance(2 * G); // skips an anchor: the streak restarts
        touch();
        o = h.obs();
        assertEq(o.resetStreak, 1);
        assertTrue(o.gapSinceVerify);
        advance(G); // the consecutive anchor: reset
        touch();
        o = h.obs();
        assertEq(o.resetStreak, 0);
        assertEq(o.outBlocks, 0);
        assertFalse(o.gapSinceVerify);
    }

    /// An out-of-range sample zeroes the streak: in, out, in over consecutive anchors does not reset.
    function test_U_clock__outOfRangeSampleZeroesTheStreak() public {
        arm();
        register(LO, HI, 0);
        refConst(5000);
        advance(G);
        touch();
        advance(G);
        touch(); // outBlocks > 0
        refConst(0);
        advance(G);
        touch();
        assertEq(h.obs().resetStreak, 1);
        refConst(5000);
        advance(G);
        touch();
        assertEq(h.obs().resetStreak, 0);
        refConst(0);
        advance(G);
        touch();
        Obs memory o = h.obs();
        assertEq(o.resetStreak, 1);
        assertGt(o.outBlocks, 0);
    }

    /// The span is closed with the cache that held over it; the new sample is committed after.
    function test_U_clock__stepOrderClosesTheSpanBeforeTheCommit() public {
        arm();
        register(LO, HI, 0);
        refConst(5000);
        advance(G);
        touch();
        Obs memory o = h.obs();
        assertEq(o.tauV, G); // credited with the old (in-range) cache
        assertEq(o.outBlocks, 0);
        assertEq(o.refTick, 5000);
        advance(G);
        touch();
        o = h.obs();
        assertEq(o.tauV, G);
        assertEq(o.outBlocks, G); // now the out-of-range cache
    }

    function test_U_clock__readRefRoundsTowardNegativeInfinity() public {
        uint32 t = uint32(vm.getBlockTimestamp());
        uint32[] memory s = new uint32[](2);
        int24[] memory k = new int24[](2);
        (s[0], s[1]) = (0, t - 300);
        (k[0], k[1]) = (0, -1);
        ref.setPath(s, k);
        (bool ok, int24 tick) = h.tryRef(); // (-300) / 600 = -0.5
        assertTrue(ok);
        assertEq(tick, -1);
        (k[0], k[1]) = (0, 1);
        ref.setPath(s, k);
        (, tick) = h.tryRef(); // +0.5
        assertEq(tick, 0);
        // the generalised window: observe([a, b]), a > b
        (ok, tick) = h.tryWindow(400, 100); // ticks 0 over [t-400, t-300], 1 over [t-300, t-100]
        assertTrue(ok);
        assertEq(tick, 0); // 200 / 300
        (ok, tick) = h.tryWindow(300, 0);
        assertEq(tick, 1);
        (ok,) = h.tryWindow(100, 100);
        assertFalse(ok);
        (ok,) = h.tryWindow(100, 200);
        assertFalse(ok);
    }

    /// The markout read over a window that ends in the past: the mean over (now - a, now - b] only, rounded toward
    /// negative infinity, independent of the ticks after the window and of the cache.
    function test_U_ref_fail__tryWindowReadsOnlyItsOwnWindow() public {
        uint32 t = uint32(vm.getBlockTimestamp());
        uint32[] memory s = new uint32[](4);
        int24[] memory k = new int24[](4);
        // -7 over (t-900, t-600], -8 over (t-600, t-450], +5000 after t-450 (outside the window read below)
        (s[0], s[1], s[2], s[3]) = (0, t - 900, t - 600, t - 450);
        (k[0], k[1], k[2], k[3]) = (0, -7, -8, 5000);
        ref.setPath(s, k);
        (bool ok, int24 tick) = h.tryWindow(900, 450);
        assertTrue(ok);
        // (-7 * 300 - 8 * 150) / 450 = -3300 / 450 = -7.33 -> -8 (toward negative infinity, not -7)
        assertEq(tick, -8);
        (ok, tick) = h.tryWindow(600, 450);
        assertTrue(ok);
        assertEq(tick, -8);
        // the read writes nothing: the cache is unchanged
        Obs memory o = h.obs();
        (ok, tick) = h.tryWindow(450, 0);
        assertEq(tick, 5000);
        assertEq(h.obs().refCachedAt, o.refCachedAt);
        assertEq(h.obs().refTick, o.refTick);
    }

    /// A window older than the reference's oracle history fails (ok = false) without reverting; one second younger
    /// succeeds.
    function test_U_ref_fail__tryWindowOlderThanHistoryFails() public {
        uint32 t = uint32(vm.getBlockTimestamp());
        uint32[] memory s = new uint32[](1);
        int24[] memory k = new int24[](1);
        (s[0], k[0]) = (t - 1000, 42);
        ref.setPath(s, k);
        (bool ok, int24 tick) = h.tryWindow(1001, 0);
        assertFalse(ok);
        assertEq(tick, 0);
        (ok, tick) = h.tryWindow(1001, 500);
        assertFalse(ok);
        (ok, tick) = h.tryWindow(1000, 0);
        assertTrue(ok);
        assertEq(tick, 42);
    }

    function test_U_clock__tickClampedToTheTickRange() public {
        refConst(TickMath.MAX_TICK);
        (, int24 tick) = h.tryRef();
        assertEq(tick, TickMath.MAX_TICK);
        refConst(TickMath.MIN_TICK);
        (, tick) = h.tryRef();
        assertEq(tick, TickMath.MIN_TICK);
    }

    // ================================================================== U-clock-gap

    function test_U_clock_gap__boundaryExactCreditsFullFreshnessWithoutGap() public {
        arm();
        refMode(MockRefPool.Mode.Revert); // the cache stays at the arming sample
        advance(2 * G); // now == refCachedAt + 2 GRID
        touch();
        Obs memory o = h.obs();
        assertEq(o.outBlocks, 2 * G);
        assertFalse(o.gapSinceVerify);
    }

    function test_U_clock_gap__plusOneTruncatesAndSetsTheGap() public {
        arm();
        refMode(MockRefPool.Mode.Revert);
        advance(2 * G + 1);
        vm.recordLogs();
        touch();
        Obs memory o = h.obs();
        assertEq(o.outBlocks, 2 * G);
        assertTrue(o.gapSinceVerify);
        assertEq(_count(vm.getRecordedLogs(), ITouchstoneHook.GapSet.selector), 1);
    }

    function test_U_clock_gap__tauVTruncatedAtTheFreshnessWindow() public {
        arm();
        register(LO, HI, 0);
        refMode(MockRefPool.Mode.Revert);
        advance(2 * G);
        touch();
        assertEq(h.obs().tauV, 2 * G);
        assertFalse(h.obs().gapSinceVerify);
        // a second, stale span earns nothing and sets the gap
        advance(1);
        touch();
        assertEq(h.obs().tauV, 2 * G);
        assertTrue(h.obs().gapSinceVerify);
    }

    /// Control: an untruncated credit of this silence would reach CURE_BLOCKS and breach.
    function test_U_clock_gap__untruncatedCreditWouldBreach() public {
        arm();
        refMode(MockRefPool.Mode.Revert);
        uint256 b0 = vm.getBlockNumber();
        advance(1000);
        touch();
        Obs memory o = h.obs();
        assertGe(vm.getBlockNumber() - b0, CURE);
        assertEq(o.outBlocks, 2 * G);
        assertEq(o.breachSeq, 0);
        assertTrue(o.gapSinceVerify);
    }

    // ================================================================== U-clock-quiet

    /// A span longer than the freshness window credits exactly the window and sets the gap, whatever the start cache,
    /// the end sample (in range, out of range or unreadable) and the pool tick. A failed read changes nothing.
    function test_U_clock_quiet__staleSpanCreditsOnlyTheFreshnessWindow(
        bool startIn,
        uint8 endMode,
        int24 poolTick,
        uint256 silence
    ) public {
        endMode = endMode % 3;
        poolTick = int24(bound(poolTick, -3000, 3000));
        silence = bound(silence, 2 * G + 1, 5000);
        arm();
        register(LO, HI, 0);
        refConst(startIn ? int24(0) : int24(5000));
        advance(G);
        touch();
        Obs memory a = h.obs();
        (uint32 gridA,,,,,) = h.state();
        assertEq(a.refCachedAt, vm.getBlockNumber());
        pool(poolTick);
        if (endMode == 2) refMode(MockRefPool.Mode.Revert);
        else refConst(endMode == 0 ? int24(0) : int24(-5000));
        advance(silence);
        touch();
        Obs memory b = h.obs();
        bool poolIn = LO < poolTick && poolTick < HI;
        assertEq(b.tauV - a.tauV, startIn && poolIn ? 2 * G : 0, "tauV");
        assertEq(b.outBlocks - a.outBlocks, startIn ? 0 : 2 * G, "outBlocks");
        assertTrue(b.gapSinceVerify);
        if (endMode == 2) {
            (uint32 gridB,,,,,) = h.state();
            assertEq(b.refTick, a.refTick);
            assertEq(b.refCachedAt, a.refCachedAt);
            assertEq(gridB, gridA);
        }
    }

    function test_U_clock_quiet__boundaryNowEqualsFreshEndFullCreditNoGap() public {
        arm();
        register(LO, HI, 0);
        refConst(5000);
        advance(2 * G);
        touch();
        Obs memory o = h.obs();
        assertEq(o.tauV, 2 * G);
        assertFalse(o.gapSinceVerify);
    }

    // ================================================================== U-clock-exit-quiet

    /// The pool exits while the cache lags in range, the market goes quiet, and a shallow re-entry is touched at a
    /// chosen hour: the silence earns nothing beyond the freshness window and the gap is set.
    function test_U_clock_exit_quiet__shallowReentryAtAChosenHourEarnsNoGapCredit(uint256 hour) public {
        hour = bound(hour, 2 * G + 1, 6000);
        arm();
        register(LO, HI, 0);
        for (uint256 i; i < 3; ++i) {
            advance(G);
            touch();
        }
        pool(1500); // exit; the reference (and the cache) still in range
        advance(G);
        touch();
        uint64 tauV0 = h.obs().tauV;
        pool(990); // shallow re-entry, touched after the silence
        refConst(990);
        advance(hour);
        touch();
        Obs memory o = h.obs();
        assertEq(o.tauV - tauV0, 2 * G);
        assertTrue(o.gapSinceVerify);
    }

    // ================================================================== U-clock-park

    /// The pool price parked exactly on either edge earns no tauV (control: by the tick alone the lower edge is in
    /// range), and the clock ignores the pool price.
    function test_U_clock_park__parkedOnEitherEdgeEarnsNothing() public {
        arm();
        register(LO, HI, 0);
        poolSqrt(TickMath.getSqrtPriceAtTick(LO));
        for (uint256 i; i < 5; ++i) {
            advance(G);
            touch();
        }
        Obs memory o = h.obs();
        assertEq(o.tauV, 0);
        assertTrue(LO <= o.tickSpot && o.tickSpot < HI); // control: a tick-based rule would earn here
        poolSqrt(TickMath.getSqrtPriceAtTick(HI));
        for (uint256 i; i < 5; ++i) {
            advance(G);
            touch();
        }
        assertEq(h.obs().tauV, 0);
        poolSqrt(TickMath.getSqrtPriceAtTick(LO) + 1);
        advance(G);
        touch();
        assertEq(h.obs().tauV, G);
        assertEq(h.obs().outBlocks, 0);
    }

    /// The upper edge in v4's other representation: a price that falls to exactly sqrtAt(hi) stores tick hi - 1
    /// (the swap stops on the boundary from above). It earns no tauV; one unit below it earns (control).
    function test_U_clock_park__upperEdgeReachedFromAboveEarnsNothing() public {
        arm();
        register(LO, HI, 0);
        h.setSlot0(TickMath.getSqrtPriceAtTick(HI), HI - 1);
        for (uint256 i; i < 5; ++i) {
            advance(G);
            touch();
        }
        Obs memory o = h.obs();
        assertEq(o.tauV, 0);
        assertEq(o.tickSpot, HI - 1); // control: a tick-based rule would earn here
        assertEq(o.outBlocks, 0); // the clock runs on the cached reference, which is in range
        h.setSlot0(TickMath.getSqrtPriceAtTick(HI) - 1, HI - 1);
        advance(G);
        touch();
        assertEq(h.obs().tauV, G);
    }

    /// The constructor rejects a tier that would brick snapshots (CURE_BLOCKS = 0 divides by zero in the latch) or
    /// switch the clock off (RESET_RUN < 2 resets on any sample), or that breaks the grid or the witness alignment.
    /// Control: the boundary values deploy.
    function test_U_clock__constructorRejectsTiersThatBreakTheClock() public {
        Create2Deployer d = new Create2Deployer();
        uint256 bt = tier.GRID_SEC / tier.GRID;
        uint32 cureMin = uint32(tier.EDGE_DELAY + 3 * tier.GRID + tier.REF_WINDOW / bt);
        Tier memory t;
        for (uint256 i; i < 9; ++i) {
            t = tier;
            if (i == 0) t.CURE_BLOCKS = 0;
            if (i == 1) t.CURE_BLOCKS = cureMin - 1;
            if (i == 2) t.RESET_RUN = 0;
            if (i == 3) t.RESET_RUN = 1;
            if (i == 4) t.GRID_SEC = 0;
            if (i == 5) t.GRID_SEC = t.GRID * uint32(bt) + 1;
            if (i == 6) t.W_TICKS = 0;
            if (i == 7) t.W_TICKS = 2 * (10 * 100 + 5); // W/2 not a multiple of the tick spacing
            if (i == 8) t.W_TICKS = -2000;
            bytes memory init = abi.encodePacked(type(TouchstoneHook).creationCode, abi.encode(_args(t, VAULT)));
            (bytes32 salt,) = d.mine(init);
            vm.expectRevert(ITouchstoneHook.BadTier.selector);
            d.deploy(init, salt);
        }
        // control: the smallest valid cure window and RESET_RUN = 2 deploy
        t = tier;
        t.CURE_BLOCKS = cureMin;
        t.RESET_RUN = 2;
        bytes memory ok = abi.encodePacked(type(TouchstoneHook).creationCode, abi.encode(_args(t, VAULT)));
        (bytes32 s0,) = d.mine(ok);
        assertTrue(d.deploy(ok, s0) != address(0));
    }

    function test_U_clock_park__parkedPriceDoesNotStopTheClock() public {
        arm();
        register(LO, HI, 0);
        poolSqrt(TickMath.getSqrtPriceAtTick(LO) + 1); // parked just inside
        refConst(5000);
        for (uint256 i; i < 11; ++i) {
            advance(G);
            touch();
        }
        Obs memory o = h.obs();
        // the first span is credited with the in-range cache it held; the rest counts out of range
        assertEq(o.tauV, G);
        assertEq(o.outBlocks, 10 * G);
        assertEq(o.breachSeq, 1);
    }

    /// After an exit the pool price can earn at most the last fresh span.
    function test_U_clock_park__atMostTwoGridsPerExit() public {
        arm();
        register(LO, HI, 0);
        for (uint256 i; i < 3; ++i) {
            advance(G);
            touch();
        }
        uint64 before = h.obs().tauV;
        poolSqrt(TickMath.getSqrtPriceAtTick(LO)); // the exit parks the price on the edge
        refMode(MockRefPool.Mode.Revert);
        for (uint256 i; i < 10; ++i) {
            advance(7);
            touch();
        }
        assertLe(h.obs().tauV - before, 2 * G);
    }

    // ================================================================== U-clock-ref

    /// Grief: a pool price held off the range does not breach a curator whose reference is in range.
    /// Control: the pool's median is out of range, so a med9 clock would breach.
    function test_U_clock_ref__griefHeldOffPoolPriceDoesNotBreach() public {
        arm();
        register(LO, HI, 0);
        pool(5000);
        for (uint256 i; i < 50; ++i) {
            advance(G);
            touch();
            // checked at every touch: in-range reference anchors reset a run every RESET_RUN anchors, so a clock
            // on the pool tick would show here as out-of-range blocks between resets
            assertEq(h.obs().outBlocks, 0);
        }
        Obs memory o = h.obs();
        assertEq(o.outBlocks, 0);
        assertEq(o.breachSeq, 0);
        assertFalse(o.med9 >= LO && o.med9 < HI); // control
    }

    /// Dodge: pushing the pool into the range does not stop a lazy curator's clock. Control: med9 is in range.
    function test_U_clock_ref__dodgePoolPushDoesNotStopTheClock() public {
        arm();
        register(LO, HI, 0);
        refConst(5000);
        pool(0);
        for (uint256 i; i < 11; ++i) {
            advance(G);
            touch();
        }
        Obs memory o = h.obs();
        assertEq(o.outBlocks, 10 * G);
        assertEq(o.breachSeq, 1);
        assertEq(o.tauV, G); // only the first span, closed with the in-range cache of the registration
        assertTrue(o.med9 >= LO && o.med9 < HI); // control
    }

    /// Parking: the pool price exactly at sqrtAt(lo) earns no tauV and does not stop the clock.
    function test_U_clock_ref__parkingEarnsNothingAndDoesNotStopTheClock() public {
        arm();
        register(LO, HI, 0);
        poolSqrt(TickMath.getSqrtPriceAtTick(LO));
        refConst(-5000);
        for (uint256 i; i < 11; ++i) {
            advance(G);
            touch();
        }
        Obs memory o = h.obs();
        assertEq(o.tauV, 0);
        assertEq(o.outBlocks, 10 * G);
        assertEq(o.breachSeq, 1);
    }

    /// Reset-wash: in-range pool ticks over many anchors cannot end a run. Control: med9 is in range throughout.
    function test_U_clock_ref__poolTicksCannotEndARun() public {
        arm();
        register(LO, HI, 0);
        refConst(5000);
        pool(0);
        for (uint256 i; i < 8; ++i) {
            advance(G);
            touch();
            assertEq(h.obs().resetStreak, 0);
        }
        Obs memory o = h.obs();
        assertEq(o.outBlocks, 7 * G);
        assertTrue(o.med9 >= LO && o.med9 < HI); // control
    }

    // ================================================================== U-ref-fail

    function test_U_ref_fail__everyModeIsFailSoftForSnapshotsAndRefresh() public {
        MockRefPool.Mode[5] memory modes = [
            MockRefPool.Mode.Old,
            MockRefPool.Mode.Revert,
            MockRefPool.Mode.Short,
            MockRefPool.Mode.GasBomb,
            MockRefPool.Mode.ReturnBomb
        ];
        arm();
        register(LO, HI, 0);
        for (uint256 i; i < modes.length; ++i) {
            refMode(modes[i]);
            Obs memory a = h.obs();
            advance(G);
            vm.recordLogs();
            snap();
            h.refreshRef();
            Vm.Log[] memory l = vm.getRecordedLogs();
            assertEq(_count(l, ITouchstoneHook.RefReadFailed.selector), 2, "snapshot read and refresh both fail");
            assertEq(_count(l, ITouchstoneHook.RefRefreshed.selector), 0);
            Obs memory b = h.obs();
            assertEq(b.refTick, a.refTick);
            assertEq(b.refCachedAt, a.refCachedAt);
            (bool ok, int24 t) = h.tryRef();
            assertFalse(ok);
            assertEq(t, 0);
            (ok,) = h.tryWindow(900, 0);
            assertFalse(ok);
            vm.expectRevert(ITouchstoneHook.RefUnreadable.selector);
            h.liveRef();
            refConst(0);
            advance(1);
            touch(); // recovers
            assertEq(h.obs().refCachedAt, vm.getBlockNumber());
        }
    }

    function test_U_ref_fail__gasStarvedRefreshRevertsItself() public {
        arm();
        advance(G);
        snap(); // the snapshot of this block has run; the anchor is pending again after a failed read
        (uint32 g,,,,,) = h.state();
        assertEq(g, vm.getBlockTimestamp() / tier.GRID_SEC);
        advance(G);
        refMode(MockRefPool.Mode.Revert);
        snap(); // read fails: the anchor stays pending
        refConst(0);
        vm.expectRevert(ITouchstoneHook.RefGas.selector);
        h.refreshRef{gas: 150_000}();
        // with enough gas the same call refreshes
        h.refreshRef();
        assertEq(h.obs().refCachedAt, vm.getBlockNumber());
    }

    /// A return-data bomb cannot charge the hook memory beyond the fixed 0x100-byte copy: reading against a
    /// 100,000-byte return costs the hook no more than the callee's own memory expansion over a short return.
    function test_U_ref_fail__returnBombDoesNotExpandTheReadersMemory() public {
        refMode(MockRefPool.Mode.Short);
        uint256 g0 = gasleft();
        h.tryRef();
        uint256 shortCost = g0 - gasleft();
        refMode(MockRefPool.Mode.ReturnBomb);
        g0 = gasleft();
        (bool ok,) = h.tryRef();
        uint256 bombCost = g0 - gasleft();
        assertFalse(ok);
        // the callee pays ~28.4k for its own 100,000-byte memory; a full copy would cost the reader as much again
        assertLt(bombCost - shortCost, 40_000);
    }

    function test_U_ref_fail__gasBombIsCappedAtRefGasCap() public {
        refMode(MockRefPool.Mode.GasBomb);
        uint256 g0 = gasleft();
        (bool ok,) = h.tryRef();
        uint256 used = g0 - gasleft();
        assertFalse(ok);
        assertLt(used, tier.REF_GAS_CAP + 20_000);
    }

    /// The constructor reads the reference: unreadable, wrong order and too little cardinality all fail closed.
    function test_U_ref_fail__armAtSeedRequiresASuccessfulRefresh() public {
        snap();
        refMode(MockRefPool.Mode.Revert);
        vm.prank(VAULT);
        vm.expectRevert(ITouchstoneHook.RefUnreadable.selector);
        h.armAtSeed();
        (,, bool armed,,,) = h.state();
        assertFalse(armed);
        refConst(0);
        vm.prank(VAULT);
        h.armAtSeed();
        vm.prank(VAULT);
        vm.expectRevert(ITouchstoneHook.Armed.selector);
        h.armAtSeed();
    }

    // ================================================================== U-ref-orient

    function test_U_ref_orient__invertedReferenceFailsTheConstructor() public {
        MockRefPool inverted = new MockRefPool(address(token1), address(token0), 500, 0, 0);
        Tier memory t = tier;
        t.REF_POOL = address(inverted);
        Create2Deployer d = new Create2Deployer();
        bytes memory init = abi.encodePacked(type(TouchstoneHook).creationCode, abi.encode(_args(t, VAULT)));
        (bytes32 salt,) = d.mine(init);
        vm.expectRevert(ITouchstoneHook.BadRef.selector);
        d.deploy(init, salt);
        // control: the same deployment with the reference in pool order succeeds
        t.REF_POOL = address(ref);
        init = abi.encodePacked(type(TouchstoneHook).creationCode, abi.encode(_args(t, VAULT)));
        (salt,) = d.mine(init);
        address hk = d.deploy(init, salt);
        assertEq(TouchstoneHook(hk).REF_POOL(), address(ref));
    }

    // ================================================================== D-3b: immutable addresses

    /// The vault and the reference are immutables: no storage word of the hook holds either address.
    function test_D_3b__vaultAndReferenceAreNotInStorage() public view {
        for (uint256 i; i < 64; ++i) {
            address w = address(uint160(uint256(vm.load(address(h), bytes32(i)))));
            assertTrue(w != address(ref) && w != VAULT, "address in storage");
        }
        assertEq(h.REF_POOL(), address(ref));
        assertEq(h.VAULT(), VAULT);
    }

    // ------------------------------------------------------------------ helpers

    function _sortedAt(int24[] memory a, uint256 i) internal pure returns (int24) {
        for (uint256 x; x < a.length; ++x) {
            for (uint256 y = x + 1; y < a.length; ++y) {
                if (a[y] < a[x]) (a[x], a[y]) = (a[y], a[x]);
            }
        }
        return a[i];
    }

    function _count(Vm.Log[] memory l, bytes32 topic) internal view returns (uint256 n) {
        for (uint256 i; i < l.length; ++i) {
            if (l[i].emitter == address(h) && l[i].topics[0] == topic) ++n;
        }
    }
}

/// @notice Anchoring on the timestamp grid at the DEMO tier (1 s blocks, GRID 300 blocks = GRID_SEC 300 s).
contract HookAnchorTest is HookBase {
    uint256 internal constant G = 300;

    function setUp() public {
        _setUpHook(true);
        refConst(0);
    }

    /// `refreshRef` after a gap closes the span with the old cache first, then commits the new sample.
    /// Control: committing first would have counted the span out of range.
    function test_U_clock_refresh_order__refreshAfterAGapClosesTheSpanWithTheOldCache() public {
        arm();
        register(-1000, 1000, 0);
        refConst(5000);
        advance(2 * G + 50);
        h.refreshRef();
        Obs memory o = h.obs();
        assertEq(o.tauV, 2 * G);
        assertEq(o.outBlocks, 0); // control
        assertTrue(o.gapSinceVerify);
        assertEq(o.refTick, 5000);
        assertEq(o.refCachedAt, vm.getBlockNumber());
    }

    /// Two callers within one anchor get the same sample, the TWAP ending at the anchor, at phases 0, 150, 299 s.
    function test_U_clock_refresh_order__twoCallersInAnAnchorGetTheSameValue() public {
        uint256[3] memory phases = [uint256(0), 150, 299];
        for (uint256 p; p < phases.length; ++p) {
            _twoCallers(phases[p]);
        }
    }

    function _twoCallers(uint256 phase) internal {
        uint256 b = (vm.getBlockNumber() / G + 2) * G;
        vm.roll(b);
        vm.warp(b + phase);
        refConst(100);
        HookHarness a = _deployHarness(tier);
        HookHarness c = _deployHarness(tier);
        a.setSlot0(TickMath.getSqrtPriceAtTick(0), 0);
        c.setSlot0(TickMath.getSqrtPriceAtTick(0), 0);
        vm.startPrank(VAULT);
        a.armAtSeed();
        c.armAtSeed();
        vm.stopPrank();
        // the next anchor starts at T; the reference moves 10 s after it
        uint256 T = (vm.getBlockTimestamp() / G + 1) * G;
        uint32[] memory s = new uint32[](2);
        int24[] memory k = new int24[](2);
        (s[0], s[1]) = (0, uint32(T + 10));
        (k[0], k[1]) = (100, 900);
        ref.setPath(s, k);
        vm.roll(vm.getBlockNumber() + (T + 5 - vm.getBlockTimestamp()));
        vm.warp(T + 5);
        a.refreshRef();
        vm.roll(vm.getBlockNumber() + 200);
        vm.warp(T + 205);
        c.refreshRef();
        assertEq(a.obs().refTick, 100);
        assertEq(c.obs().refTick, a.obs().refTick);
        (uint32 ga,,,,,) = a.state();
        (uint32 gc,,,,,) = c.state();
        assertEq(ga, T / G);
        assertEq(gc, ga);
    }

    /// At phase 150 s one anchor spans two block grids; it is refreshed once.
    function test_U_clock_refresh_order__oneAnchorAcrossTwoBlockGridsRefreshesOnce() public {
        uint256 b0 = (vm.getBlockNumber() / G + 2) * G; // a block-grid boundary
        vm.roll(b0 - 160);
        vm.warp(b0 - 160 + 150);
        arm(); // anchor (b0 - 10) / 300 = b0/300 - 1
        vm.roll(b0 - 100);
        vm.warp(b0 - 100 + 150); // ts = b0 + 50: anchor b0/300, block grid b0/300 - 1
        vm.recordLogs();
        h.refreshRef();
        assertEq(h.obs().refCachedAt, b0 - 100); // the anchor is new at this block
        vm.roll(b0 + 100);
        vm.warp(b0 + 100 + 150); // ts = b0 + 250: same anchor, block grid b0/300
        h.refreshRef();
        Vm.Log[] memory l = vm.getRecordedLogs();
        uint256 n;
        for (uint256 i; i < l.length; ++i) {
            if (l[i].emitter == address(h) && l[i].topics[0] == ITouchstoneHook.RefRefreshed.selector) ++n;
        }
        assertEq(n, 1);
        (uint32 g,,,,,) = h.state();
        assertEq(g, b0 / G);
    }
}
