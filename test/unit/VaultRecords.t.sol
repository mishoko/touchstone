// SPDX-License-Identifier: MIT
pragma solidity 0.8.26;

import {Vm} from "forge-std/Vm.sol";
import {HarnessVaultBase} from "../utils/HarnessVaultBase.sol";
import {Scalars} from "../utils/VaultHarness.sol";
import {ITouchstoneHook} from "../../src/interfaces/ITouchstoneHook.sol";
import {IVaultEntries} from "../../src/interfaces/ITouchstoneVaultExtension.sol";
import {Obs} from "../../src/types/Obs.sol";
import {PreSwap} from "../../src/types/PreSwap.sol";
import {V} from "../../src/types/Bank.sol";
import {Ladder} from "../../src/libraries/Ladder.sol";
import {VaultCommon} from "../../src/TouchstoneVaultBase.sol";
import {
    EpochRecord,
    F_STALE,
    F_WITNESS_MISSING,
    F_BREACH,
    F_REF_STALE,
    F_DEFERRED,
    F_UNVERIFIED
} from "../../src/types/EpochRecord.sol";
import {MockRefPool} from "../utils/MockRefPool.sol";
import {VaultHarness, CheckpointLog} from "../utils/VaultHarness.sol";
import {NoFloorExtension} from "../utils/NoFloorExtension.sol";

/// @notice The epoch records (spec §3.9, §4.6 step (6), §4.12 `roll`), `ModeChanged` (§4.9) and the gas-starved
///         `poke` (F-048 (b)). Real flow (factory deployment, real hook, scripted reference pool, TEST tier).
abstract contract VaultRecordsBase is HarnessVaultBase {
    function setUp() public virtual override {
        super.setUp();
        _fundUser(alice, 1e24);
        _deposit(alice, 1e23);
    }

    /// @dev A placed vault, its witness rolled, then a deposit's checkpoint rolls into a fresh epoch (its record opens
    ///      with `witnessMissing`; no poke yet).
    function prepareSweep() external {
        hv.placeForTest(-600, 600);
        _keepAlive(20);
        IVaultEntries(vault).poke();
        advance(tier.EPOCH);
        _deposit(alice, 1e18);
    }
}

contract VaultRecordsTest is VaultRecordsBase {
    bytes32 internal constant T_REC = keccak256("EpochRecorded(uint32,(uint40,uint40,int24,int24,uint64,uint64,uint192,uint8,uint96,uint96,uint128,uint128,uint128,uint128,uint96))");
    bytes32 internal constant T_MODE = keccak256("ModeChanged(uint8,uint8)");

    function _ve() internal view returns (IVaultEntries) {
        return IVaultEntries(vault);
    }

    function _epochNow() internal view returns (uint32) {
        return hv.scalars().epoch;
    }

    function _none() internal pure returns (PreSwap memory) {
        return PreSwap(false, false, 0);
    }

    /// @dev A real curator placement on [lo, hi) (δ₀ > 0, so base accrues), its markout settled.
    function _place(int24 lo, int24 hi) internal {
        hv.placeForTest(lo, hi);
        advance(tier.COOLDOWN);
        vm.prank(curator);
        hv.setPositions(lo, hi, _none(), 0, type(uint256).max);
        assertGt(hv.scalars().delta0, 0, "precondition: a real placement deploys");
        _keepAlive(tier.H_MARKOUT + 20);
        hv.settle();
    }

    function _count(Vm.Log[] memory logs, bytes32 t) internal view returns (uint256 n) {
        for (uint256 i; i < logs.length; ++i) {
            if (logs[i].emitter == vault && logs[i].topics[0] == t) ++n;
        }
    }

    function _modeEvent(Vm.Log[] memory logs) internal view returns (bool found, uint8 from, uint8 to) {
        for (uint256 i; i < logs.length; ++i) {
            if (logs[i].emitter == vault && logs[i].topics[0] == T_MODE) {
                require(!found, "two ModeChanged in one call");
                found = true;
                (from, to) = abi.decode(logs[i].data, (uint8, uint8));
            }
        }
    }

    // ------------------------------------------------------------------ the roll (U-gap's record clause)

    /// @notice A gap of V + 3 epochs: the poke's checkpoint writes exactly two records, the stored epoch (closed at this
    ///         block, flagged `stale`, τ_v and τ_b replaced by the differences over its window) and one gap record
    ///         (`old + 1`, an empty window at this block, `stale` and `witnessMissing`); the new record opens at this
    ///         block and the poke's own witness roll clears its `witnessMissing` (control for the unpoked epoch below).
    function test_U_gap__atMostTwoRecordsAcrossAGap() public {
        hv.placeForTest(-600, 600);
        _keepAlive(20);
        uint32 e0 = _epochNow();
        EpochRecord memory open0 = hv.record(e0);
        assertEq(open0.end, 0, "precondition: the record is open");
        assertGt(open0.start, 0, "the first record opened at seed");
        vm.recordLogs();
        advance((V + 3) * tier.EPOCH);
        _ve().poke();
        Vm.Log[] memory logs = vm.getRecordedLogs();
        uint32 ne = _epochNow();
        assertEq(ne, uint32(vm.getBlockNumber() / tier.EPOCH), "the epoch advanced to now");
        assertGt(ne, e0 + 1, "precondition: epochs were skipped");
        assertEq(_count(logs, T_REC), 2, "at most two records");
        uint256 seen;
        for (uint256 i; i < logs.length; ++i) {
            if (logs[i].emitter == vault && logs[i].topics[0] == T_REC) {
                assertEq(uint256(logs[i].topics[1]), e0 + seen, "old, then the gap record");
                ++seen;
            }
        }
        Obs memory o = _obs();
        EpochRecord memory r0 = hv.record(e0);
        assertEq(r0.end, vm.getBlockNumber(), "old closed at this block");
        assertTrue(r0.flags & F_STALE != 0, "old spans skipped epochs: stale");
        assertEq(r0.tauV, o.tauV - open0.tauV, "tauV over the window");
        assertEq(r0.tauB, o.tauB - open0.tauB, "tauB over the window");
        assertEq(r0.med9End, o.med9, "med9 at the end");
        EpochRecord memory rg = hv.record(e0 + 1);
        assertEq(rg.start, vm.getBlockNumber(), "gap: start");
        assertEq(rg.end, vm.getBlockNumber(), "gap: empty window");
        assertEq(rg.flags, F_STALE | F_WITNESS_MISSING, "gap: stale, witness missing");
        assertEq(rg.lTau + rg.base + rg.minted, 0, "gap: nothing accumulated");
        EpochRecord memory rn = hv.record(ne);
        assertEq(rn.start, vm.getBlockNumber(), "new: opened now");
        assertEq(rn.end, 0, "new: open");
        assertEq(rn.tauV, o.tauV, "new: the counter at start");
        assertEq(rn.flags & F_WITNESS_MISSING, 0, "the poke rolled a live witness for the epoch");
    }

    /// @notice An epoch nobody pokes closes with `witnessMissing` (S19D-02): the roll into it opens the record with the
    ///         flag, and only a successful roll with `L_W > 0` clears it. Control: the poked epoch of the test above.
    function test_U_record__unpokedEpochClosesWitnessMissing() public {
        hv.placeForTest(-600, 600);
        _keepAlive(20);
        advance(tier.EPOCH);
        _deposit(alice, 1e18); // the checkpoint rolls into e1; no poke
        uint32 e1 = _epochNow();
        assertTrue(hv.record(e1).flags & F_WITNESS_MISSING != 0, "opened missing");
        advance(tier.EPOCH);
        _deposit(alice, 1e18); // closes e1, still unpoked
        EpochRecord memory r = hv.record(e1);
        assertGt(r.end, 0, "closed");
        assertTrue(r.flags & F_WITNESS_MISSING != 0, "an unpoked epoch closes with its witness missing");
        // control: poked, the next one closes clear
        uint32 e2 = _epochNow();
        _ve().poke();
        advance(tier.EPOCH);
        _deposit(alice, 1e18);
        assertEq(hv.record(e2).flags & F_WITNESS_MISSING, 0, "control: a poked epoch closes clear");
    }

    /// @notice A successful roll with `L_W = 0` (an unfunded witness) leaves the record's `witnessMissing` set; control:
    ///         the funded roll of `test_U_gap__atMostTwoRecordsAcrossAGap` clears it.
    function test_U_record__unfundedWitnessStaysMissing() public {
        hv.placeForTest(-600, 600);
        _keepAlive(20);
        _ve().poke();
        // the hook's witness reserves emptied (slot 4 of the hook's layout snapshot): the next roll sizes L_W = 0
        vm.store(hook, bytes32(uint256(4)), bytes32(0));
        advance(tier.EPOCH);
        vm.recordLogs();
        _ve().poke();
        Vm.Log[] memory logs = vm.getRecordedLogs();
        bool rolledEmpty;
        for (uint256 i; i < logs.length; ++i) {
            if (logs[i].emitter == hook && logs[i].topics[0] == keccak256("WitnessRolled(uint32,uint256,uint256,uint128)")) {
                (,,, uint128 lw) = abi.decode(logs[i].data, (uint32, uint256, uint256, uint128));
                rolledEmpty = lw == 0;
            }
        }
        assertTrue(rolledEmpty, "precondition: the roll sized no witness");
        assertTrue(hv.record(_epochNow()).flags & F_WITNESS_MISSING != 0, "an unfunded witness is missing");
    }

    // ------------------------------------------------------------------ the accumulators

    /// @notice The removal breach (the third, processed at once) cuts the segment at `b_last`: the cut span's exposure is
    ///         in `lTau` (so `lTau = L·Δτ_v` exactly) and its base in both `base` and `forfeitValue` (eligibility read
    ///         before the op, which clears `placedByCurator`; S19D-01, S19D2-02); the forfeited shares and the `breach`
    ///         flag; one `ModeChanged(NORMAL, EXITED)`.
    function test_U_record__removalBreachCutInLTauAndBase() public {
        _place(-1000, 1000);
        _keepAlive(200); // in range: base accrues for the open segment
        refPool.setTick(1600); // the reference leaves the range
        _driveBreaches(3);
        uint32 e = _epochNow();
        EpochRecord memory b0 = hv.record(e);
        Scalars memory s = hv.scalars();
        uint128 L = hv.holdings().L;
        uint256 unvested = _unvested();
        vm.recordLogs();
        _ve().processBreach();
        Vm.Log[] memory logs = vm.getRecordedLogs();
        Obs memory o = _obs();
        EpochRecord memory r = hv.record(e);
        assertEq(hv.bankFull().mode, Ladder.EXITED, "removed");
        uint256 cutTau = o.tauVAtLastPending - s.tauV0;
        assertGt(cutTau, 0, "precondition: an in-range cut span");
        assertEq(r.lTau - b0.lTau, uint256(L) * (o.tauV - s.tauV0), "lTau = L * dtauV across the cut");
        uint256 cut = r.base - b0.base;
        assertGt(cut, 0, "the cut span's base is in the gross base (eligibility before the op)");
        assertEq(r.forfeitValue - b0.forfeitValue, cut + s.baseOwed, "forfeitValue: the cut and the deferred base");
        assertEq(r.forfeited - b0.forfeited, unvested, "the forfeited shares");
        assertTrue(r.flags & F_BREACH != 0, "breach");
        (bool found, uint8 from, uint8 to) = _modeEvent(logs);
        assertTrue(found && from == Ladder.NORMAL && to == Ladder.EXITED, "ModeChanged(NORMAL, EXITED)");
    }

    /// @notice §3.8/§3.9: a closed record's `tauV` and `lTau` are rebuilt from the `Checkpoint` events of its window
    ///         (each event's L is the L of the segment it closes): the counter at the opening checkpoint, then
    ///         Σ L_i·(τ_i − τ_{i−1}) over the window's checkpoints up to the one that rolls it out, across L changes.
    function test_U_record__tauVAndLTauRebuiltFromTheWindowsCheckpoints() public {
        _place(-1000, 1000);
        advance(tier.EPOCH - vm.getBlockNumber() % tier.EPOCH + 1);
        ITouchstoneHook(hook).refreshRef();
        vm.recordLogs();
        hv.settle(); // rolls into e: the record opens here
        Vm.Log[] memory open = vm.getRecordedLogs();
        uint32 e = _epochNow();
        uint64 tauPrev = _lastCheckpoint(open).tauV;
        assertEq(hv.record(e).tauV, tauPrev, "the open record holds the counter at the opening checkpoint");
        vm.recordLogs();
        for (uint256 i; i < 3; ++i) {
            _keepAlive(20);
            _deposit(alice, 1e21 * (i + 1)); // L changes after each checkpoint
        }
        advance(tier.EPOCH - vm.getBlockNumber() % tier.EPOCH + 1);
        ITouchstoneHook(hook).refreshRef();
        hv.settle(); // rolls out of e
        Vm.Log[] memory logs = vm.getRecordedLogs();
        uint256 lt;
        uint256 n;
        for (uint256 i; i < logs.length; ++i) {
            if (logs[i].emitter != vault || logs[i].topics[0] != T_CHECKPOINT) continue;
            CheckpointLog memory c = abi.decode(logs[i].data, (CheckpointLog));
            lt += uint256(c.L) * (c.tauV - tauPrev);
            tauPrev = c.tauV;
            ++n;
        }
        EpochRecord memory r = hv.record(e);
        assertEq(n, 4, "the window's checkpoints");
        assertGt(r.end, 0, "closed");
        assertEq(r.tauV, tauPrev - _lastCheckpoint(open).tauV, "tauV from the events");
        assertGt(r.lTau, 0, "precondition: exposure in the window");
        assertEq(r.lTau, lt, "lTau = sum of L_i * dtau_i from the events");
    }

    bytes32 internal constant T_CHECKPOINT =
        keccak256("Checkpoint(uint64,uint64,uint256,uint64,uint256,uint128,int24,int24)");

    function _lastCheckpoint(Vm.Log[] memory logs) internal view returns (CheckpointLog memory c) {
        for (uint256 i; i < logs.length; ++i) {
            if (logs[i].emitter == vault && logs[i].topics[0] == T_CHECKPOINT) {
                c = abi.decode(logs[i].data, (CheckpointLog));
            }
        }
    }

    function _unvested() internal view returns (uint256 u) {
        (uint128 c, uint128 mp,,,,,) = hv.bank();
        return uint256(c) + mp;
    }

    /// @notice A deferral sets `deferred` and `refStale` and adds the segment's base to `base` without a mint; the next
    ///         priced checkpoint mints it into `minted`.
    function test_U_record__deferralThenMint() public {
        _place(-1000, 1000);
        // the start of an epoch, so the deferral and the mint below fall in one epoch (TEST: EPOCH 120, GRID 20)
        advance(tier.EPOCH - vm.getBlockNumber() % tier.EPOCH + 1);
        ITouchstoneHook(hook).refreshRef();
        hv.settle(); // a checkpoint rolls the stored epoch to this one
        _keepAlive(20);
        uint32 e = _epochNow();
        EpochRecord memory b0 = hv.record(e);
        advance(3 * tier.GRID); // the cache goes stale
        refPool.setMode(MockRefPool.Mode.Revert);
        _deposit(alice, 1e18);
        assertEq(_epochNow(), e, "precondition: the same epoch");
        EpochRecord memory r = hv.record(e);
        assertTrue(r.flags & (F_DEFERRED | F_REF_STALE) == (F_DEFERRED | F_REF_STALE), "deferred, refStale");
        assertTrue(r.flags & F_UNVERIFIED != 0, "unverified: the stale span set the gap");
        assertGt(r.base, b0.base, "the base accrued");
        assertEq(r.minted, b0.minted, "nothing minted");
        assertGt(hv.scalars().baseOwed, 0, "precondition: deferred");
        refPool.setMode(MockRefPool.Mode.Ok);
        ITouchstoneHook(hook).refreshRef();
        uint256 S = hv.totalSupply();
        _deposit(alice, 1e18);
        assertEq(_epochNow(), e, "precondition: still the same epoch");
        assertEq(hv.scalars().baseOwed, 0, "minted");
        uint256 m = hv.record(e).minted - r.minted;
        assertGt(m, 0, "the mint is recorded");
        assertEq(hv.totalSupply(), S + 1e18 + m, "the supply grew by the deposit and the recorded mint");
    }

    /// @notice An action adds its immediate charge and its burns (`Op.Charge`'s bank and bond parts) to the record of
    ///         the stored epoch, as its `Action` event reports them.
    function test_U_record__actionRecordsItsChargeAndBurns() public {
        hv.placeForTest(-1000, 1000);
        refPool.setTick(40); // the reference off spot: a positive charge
        _keepAlive(4 * tier.GRID);
        advance(tier.COOLDOWN);
        uint32 e = _epochNow();
        EpochRecord memory b0 = hv.record(e);
        vm.recordLogs();
        vm.prank(curator);
        hv.setPositions(-1000, 1000, _none(), 0, type(uint256).max);
        Vm.Log[] memory logs = vm.getRecordedLogs();
        assertEq(_epochNow(), e, "precondition: the same epoch");
        uint256 charge;
        uint256 bankB;
        uint256 bondB;
        for (uint256 i; i < logs.length; ++i) {
            if (logs[i].emitter == vault && logs[i].topics[0] == VaultCommon.Action.selector) {
                (, charge,, bankB, bondB) = abi.decode(logs[i].data, (uint8, uint256, uint160, uint256, uint256));
            }
        }
        assertGt(charge, 0, "precondition: a charge");
        EpochRecord memory r = hv.record(e);
        assertEq(r.charge - b0.charge, charge, "the charge");
        assertEq(r.bankBurned - b0.bankBurned, bankB, "the bank's burn");
        assertEq(r.bondBurned - b0.bondBurned, bondB, "the bond's burn");
    }

    // ------------------------------------------------------------------ ModeChanged (F-048 (c))

    /// @notice `ModeChanged` exactly at each change: a breach NORMAL → BREACHED, a placement BREACHED → NORMAL, a retire
    ///         NORMAL → EXITED; none for a placement NORMAL → NORMAL or a breach EXITED → EXITED.
    function test_U_record__modeChangedAtEveryChangeAndOnlyThen() public {
        _place(-1000, 1000);
        // a placement NORMAL -> NORMAL: none
        advance(tier.COOLDOWN);
        vm.recordLogs();
        vm.prank(curator);
        hv.setPositions(-1000, 1000, _none(), 0, type(uint256).max);
        (bool found,,) = _modeEvent(vm.getRecordedLogs());
        assertFalse(found, "NORMAL -> NORMAL: no event");
        _keepAlive(tier.H_MARKOUT + 20);
        hv.settle();
        // a breach NORMAL -> BREACHED
        refPool.setTick(1600);
        _driveBreaches(1);
        vm.recordLogs();
        _ve().processBreach();
        (bool f1, uint8 a1, uint8 b1) = _modeEvent(vm.getRecordedLogs());
        assertTrue(f1 && a1 == Ladder.NORMAL && b1 == Ladder.BREACHED, "NORMAL -> BREACHED");
        // a placement BREACHED -> NORMAL
        refPool.setTick(0);
        _keepAlive(4 * tier.GRID);
        advance(tier.COOLDOWN);
        vm.recordLogs();
        vm.prank(curator);
        hv.setPositions(-1000, 1000, _none(), 0, type(uint256).max);
        (bool f2, uint8 a2, uint8 b2) = _modeEvent(vm.getRecordedLogs());
        assertTrue(f2 && a2 == Ladder.BREACHED && b2 == Ladder.NORMAL, "BREACHED -> NORMAL");
        _keepAlive(tier.H_MARKOUT + 20);
        hv.settle();
        // retire NORMAL -> EXITED
        vm.recordLogs();
        vm.prank(curator);
        _ve().retire();
        (bool f3, uint8 a3, uint8 b3) = _modeEvent(vm.getRecordedLogs());
        assertTrue(f3 && a3 == Ladder.NORMAL && b3 == Ladder.EXITED, "NORMAL -> EXITED");
        // a breach in EXITED: none
        refPool.setTick(1600);
        _driveBreaches(1);
        vm.recordLogs();
        _ve().processBreach();
        assertEq(hv.bankFull().breaches, 2, "precondition: the breach was processed");
        (bool f4,,) = _modeEvent(vm.getRecordedLogs());
        assertFalse(f4, "EXITED -> EXITED: no event");
    }

    // ------------------------------------------------------------------ the gas-starved poke (F-048 (b))

    /// @notice For every gas limit (1k steps), a `poke` into a fresh epoch either reverts or rolls the witness: it never
    ///         completes with the roll failed (the record's `witnessMissing` left set). Control: the same sweep with the
    ///         floor removed through the extension's seam (`VaultRecordsNoFloorTest`) completes some pokes with the roll
    ///         failed.
    function test_U_record__gasStarvedPokeNeverRecordsAFailedRoll() public {
        (uint256 reverted, uint256 rolled, uint256 failed) = PokeSweep.sweep(this, hv, vault);
        assertEq(failed, 0, "no completed poke leaves the roll failed");
        assertGt(reverted, 0, "non-vacuous: some limits revert");
        assertGt(rolled, 0, "non-vacuous: some limits roll");
    }
}

/// @notice The sweep shared by the guarded test and its control.
library PokeSweep {
    function sweep(VaultRecordsBase t, VaultHarness hv, address vault)
        internal
        returns (uint256 reverted, uint256 rolled, uint256 failed)
    {
        t.prepareSweep();
        uint32 e = hv.scalars().epoch;
        Vm vm = Vm(address(uint160(uint256(keccak256("hevm cheat code")))));
        uint256 snap = vm.snapshotState();
        for (uint256 g = 150_000; g <= 900_000; g += 1_000) {
            (bool ok,) = vault.call{gas: g}(abi.encodeCall(IVaultEntries.poke, ()));
            if (!ok) ++reverted;
            else if (hv.record(e).flags & F_WITNESS_MISSING != 0) ++failed;
            else ++rolled;
            vm.revertToState(snap);
        }
    }
}

/// @notice The sweep's control: the extension without the floor (`NoFloorExtension`).
contract VaultRecordsNoFloorTest is VaultRecordsBase {
    function _extCode() internal pure override returns (bytes memory) {
        return type(NoFloorExtension).creationCode;
    }

    function test_U_record__ctlNoFloorCompletesAPokeWithAFailedRoll() public {
        (,, uint256 failed) = PokeSweep.sweep(this, hv, vault);
        assertGt(failed, 0, "control: without the floor some limit completes the poke with the roll failed");
    }
}
