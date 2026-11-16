// SPDX-License-Identifier: MIT
pragma solidity 0.8.26;

import {Vm} from "forge-std/Vm.sol";
import {ScriptedVaultBase} from "../utils/ScriptedVault.sol";
import {VaultHarness, Scalars, CheckpointLog} from "../utils/VaultHarness.sol";
import {Obs} from "../../src/types/Obs.sol";
import {Bank, Cohort, RING} from "../../src/types/Bank.sol";
import {Ladder} from "../../src/libraries/Ladder.sol";

/// @notice The vault's checkpoint (breach-first; steps 1-6) on the scripted harness: the vest gate and its live leg,
///         the order of drip, breach processing, the (inert) accrual step, vesting and the epoch roll.
contract VaultCheckpointTest is ScriptedVaultBase {
    // ------------------------------------------------------------------ the vest gate

    function test_U_vest_gate__openGateVestsAMaturedCohort() public {
        VaultHarness h = _placedHarness();
        _setCohort(h, 5, 7e18, 0); // vestBlock = (5 + 1 + V)·EPOCH = 1200
        h.checkpoint(_obsIn(), true, 0);
        Bank memory b = h.bankFull();
        assertEq(b.vestedUnclaimed, 7e18, "vested");
        assertEq(b.maturedPending, 0, "maturedPending");
        assertEq(h.liveReads(), 1, "one live read");
    }

    function test_U_vest_gate__liveReferenceOutBlocksTheVest() public {
        VaultHarness h = _placedHarness();
        _setCohort(h, 5, 7e18, 0);
        h.checkpoint(_obsIn(), true, 600); // the cache is in range, the live reference is at the upper edge (out)
        _assertHeldMatured(h, 7e18);
        assertEq(h.liveReads(), 1, "the live leg was read");
    }

    function test_U_vest_gate__failedLiveReadBlocksTheVest() public {
        VaultHarness h = _placedHarness();
        _setCohort(h, 5, 7e18, 0);
        h.checkpoint(_obsIn(), false, 0); // the failed read's tick field (0) lies inside the range
        _assertHeldMatured(h, 7e18);
    }

    function test_U_vest_gate__gapBlocksTheVestWithoutReading() public {
        VaultHarness h = _placedHarness();
        _setCohort(h, 5, 7e18, 0);
        Obs memory o = _obsIn();
        o.gapSinceVerify = true;
        h.checkpoint(o, true, 0);
        _assertHeldMatured(h, 7e18);
        assertEq(h.liveReads(), 0, "no live read behind a closed cache leg");
    }

    function test_U_vest_gate__staleCacheBlocksTheVest() public {
        VaultHarness h = _placedHarness();
        _setCohort(h, 5, 7e18, 0);
        Obs memory o = _obsIn();
        o.refFresh = false;
        h.checkpoint(o, true, 0);
        _assertHeldMatured(h, 7e18);
        assertEq(h.liveReads(), 0, "no live read");
    }

    function test_U_vest_gate__cacheOutOfRangeBlocksTheVest() public {
        VaultHarness h = _placedHarness();
        _setCohort(h, 5, 7e18, 0);
        Obs memory o = _obsIn();
        o.refTick = 600; // the upper edge is outside [lo, hi)
        h.checkpoint(o, true, 0);
        _assertHeldMatured(h, 7e18);
        o.refTick = -601;
        h.checkpoint(o, true, 0);
        _assertHeldMatured(h, 7e18);
    }

    function test_U_vest_gate__emptyRangeBlocksTheVest() public {
        VaultHarness h = _placedHarness();
        _setCohort(h, 5, 7e18, 0);
        Obs memory o = _obsIn();
        o.emptyRange = true;
        h.checkpoint(o, true, 0);
        _assertHeldMatured(h, 7e18);
    }

    function test_U_vest_gate__onlyMaturedPendingReadsTheLiveLegAndVests() public {
        VaultHarness h = _placedHarness();
        _setCohort(h, 9, 0, 3e18); // nothing due by time; only maturedPending is vestable
        h.checkpoint(_obsIn(), true, 0);
        assertEq(h.liveReads(), 1, "maturedPending makes the live leg due");
        assertEq(h.bankFull().vestedUnclaimed, 3e18, "maturedPending vests with the gate open");
    }

    function test_U_vest_gate__onlyMaturedPendingWithTheLiveReferenceOutStaysPending() public {
        VaultHarness h = _placedHarness();
        _setCohort(h, 9, 0, 3e18);
        h.checkpoint(_obsIn(), true, -601);
        assertEq(h.liveReads(), 1, "read");
        assertEq(h.bankFull().maturedPending, 3e18, "still pending");
        assertEq(h.bankFull().vestedUnclaimed, 0, "nothing vested");
    }

    function test_U_vest_gate__nothingVestableReadsNothing() public {
        VaultHarness h = _placedHarness();
        _setCohort(h, 9, 7e18, 0); // vestBlock = 1680 > 1200
        h.checkpoint(_obsIn(), true, 0);
        assertEq(h.liveReads(), 0, "the live leg is read only when something is vestable");
        Bank memory b = h.bankFull();
        assertEq(b.cohorts[9 % RING].shares, 7e18, "cohort kept");
        assertEq(b.vestedUnclaimed + b.maturedPending, 0, "nothing moved");
    }

    // ------------------------------------------------------------------ breach processing (step 3)

    function test_U_forfeit__aPendingBreachForfeitsBeforeTheVest() public {
        VaultHarness h = _placedHarness();
        _setCohort(h, 5, 7e18, 0); // matured, and the gate would be open
        Obs memory o = _obsIn();
        (o.pendingBreaches, o.breachSeq, o.lastPendingBlock, o.tauVAtLastPending) = (1, 1, 1190, 50);
        h.checkpoint(o, true, 0);
        Bank memory b = h.bankFull();
        assertEq(b.vestedUnclaimed, 0, "nothing vests past a pending breach");
        assertEq(b.forfeitPool, 7e18, "the matured cohort is forfeited");
        assertEq(b.breaches, 1, "breaches");
        assertEq(b.mode, Ladder.BREACHED, "mode");
    }

    function test_U_forfeit__breachThenSegmentEndsAtTheCheckpoint() public {
        VaultHarness h = _placedHarness();
        Obs memory o = _obsIn();
        (o.pendingBreaches, o.breachSeq, o.lastPendingBlock, o.tauVAtLastPending) = (1, 1, 1190, 50);
        o.tauV = 60;
        vm.recordLogs();
        h.checkpoint(o, true, 0);
        Scalars memory s = h.scalars();
        // step (3) sets τ_v0 := τ_v at the last latch and segStart := b_last; step (4) then closes the segment at now
        assertEq(s.tauV0, 60, "tauV0 := tauV after the (inert) accrual step");
        assertEq(s.segStart, 1200, "segStart := now");
        Vm.Log[] memory logs = vm.getRecordedLogs();
        bytes32[] memory order = _topics(logs);
        assertEq(order.length, 3, "three checkpoint events");
        assertEq(order[0], T_ACK, "ackBreaches first");
        assertEq(order[1], T_BREACH, "then BreachProcessed");
        assertEq(order[2], T_CHECKPOINT, "then Checkpoint");
    }

    function test_U_forfeit__everyPendingBreachIsCountedAndAcknowledged() public {
        VaultHarness h = _placedHarness();
        Obs memory o = _obsIn();
        (o.pendingBreaches, o.breachSeq, o.lastPendingBlock, o.tauVAtLastPending) = (2, 2, 1190, 50);
        vm.recordLogs();
        h.checkpoint(o, true, 0);
        Bank memory b = h.bankFull();
        assertEq(b.breaches, 2, "two breaches counted");
        assertEq(b.mode, Ladder.BREACHED, "BREACHED below three");
        Vm.Log[] memory logs = vm.getRecordedLogs();
        for (uint256 i; i < logs.length; ++i) {
            if (logs[i].topics[0] == T_ACK) assertEq(abi.decode(logs[i].data, (uint32)), 2, "ackBreaches(2)");
            if (logs[i].topics[0] == T_BREACH) {
                (uint32 n, uint40 last,) = abi.decode(logs[i].data, (uint32, uint40, uint256));
                assertEq(n, 2, "count");
                assertEq(last, 1190, "lastBlock");
            }
        }
        // a third breach removes the curator
        vm.roll(1201);
        o = _obsIn();
        (o.pendingBreaches, o.breachSeq, o.lastPendingBlock, o.tauVAtLastPending) = (1, 3, 1200, 50);
        h.checkpoint(o, true, 0);
        b = h.bankFull();
        assertEq(b.breaches, 3, "three");
        assertEq(b.mode, Ladder.EXITED, "EXITED at three");
        assertEq(b.exitBlock, 1201, "exitBlock := now");
    }

    // ------------------------------------------------------------------ drip (step 2), accrual (step 4), epoch roll (step 6)

    function test_U_forfeit__dripBurnsHalfAtHalfTheDripLength() public {
        VaultHarness h = _placedHarness();
        Bank memory b = h.bankFull();
        (b.forfeitPool, b.forfeitTotal, b.forfeitStart) = (120e18, 120e18, 1140);
        b.bondShares -= 120e18;
        h.setBank(b);
        uint256 s0 = h.totalSupply();
        h.checkpoint(_obsIn(), true, 0);
        assertEq(h.bankFull().forfeitPool, 60e18, "half the pool remains after 60 of 120 blocks");
        assertEq(s0 - h.totalSupply(), 60e18, "the dripped shares are burned");
    }

    function test_U_vest_gate__inertAccrualClosesTheSegmentAndEmitsCheckpoint() public {
        VaultHarness h = _placedHarness();
        Obs memory o = _obsIn();
        o.tauV = 77;
        o.tauB = 33;
        vm.recordLogs();
        h.checkpoint(o, true, 0);
        Scalars memory s = h.scalars();
        assertEq(s.tauV0, 77, "tauV0 := tauV");
        assertEq(s.segStart, 1200, "segStart := now");
        assertEq(s.baseOwed, 0, "no base owed");
        Vm.Log[] memory logs = vm.getRecordedLogs();
        assertEq(logs.length, 1, "one event");
        assertEq(logs[0].topics[0], T_CHECKPOINT, "Checkpoint");
        CheckpointLog memory g = abi.decode(logs[0].data, (CheckpointLog));
        assertEq(g.tauV, 77);
        assertEq(g.tauB, 33);
        assertEq(g.nav0, s.nav0);
        assertEq(g.delta, 0);
        assertEq(g.base, 0, "accrual is inert in this build");
        assertEq(g.L, 0);
        assertEq(g.lo, -600);
        assertEq(g.hi, 600);
    }

    function test_U_vest_gate__epochRollResetsTheSwapCounters() public {
        VaultHarness h = _placedHarness();
        h.setScalars(2e18, 0, 0, 1150, 0, 9, 4e16, 3); // the previous epoch's counters
        h.checkpoint(_obsIn(), true, 0);
        Scalars memory s = h.scalars();
        assertEq(s.epoch, 10, "epoch advanced");
        assertEq(s.epochSwapFrac, 0, "epochSwapFrac reset");
        assertEq(s.swapCount, 0, "swapCount reset");
        // within the same epoch the counters stay
        h.setScalars(2e18, 0, 0, 1150, 0, 10, 4e16, 3);
        vm.roll(1201);
        h.checkpoint(_obsIn(), true, 0);
        s = h.scalars();
        assertEq(s.epoch, 10, "same epoch");
        assertEq(s.epochSwapFrac, 4e16, "kept");
        assertEq(s.swapCount, 3, "kept");
    }

    // ------------------------------------------------------------------ helpers

    function _assertHeldMatured(VaultHarness h, uint256 amount) internal view {
        Bank memory b = h.bankFull();
        assertEq(b.vestedUnclaimed, 0, "nothing vests with the gate closed");
        assertEq(b.maturedPending, amount, "the matured cohort waits in maturedPending");
    }

    function _topics(Vm.Log[] memory logs) internal pure returns (bytes32[] memory out) {
        uint256 n;
        out = new bytes32[](logs.length);
        for (uint256 i; i < logs.length; ++i) {
            bytes32 t = logs[i].topics[0];
            if (t == T_ACK || t == T_BREACH || t == T_CHECKPOINT || t == T_DEFERRED) out[n++] = t;
        }
        assembly {
            mstore(out, n)
        }
    }
}
