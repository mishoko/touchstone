// SPDX-License-Identifier: MIT
pragma solidity 0.8.26;

import {Vm} from "forge-std/Vm.sol";
import {VectorBase} from "../utils/VectorBase.sol";
import {ScriptedVaultBase} from "../utils/ScriptedVault.sol";
import {VaultHarness, Scalars, CheckpointLog} from "../utils/VaultHarness.sol";
import {Obs} from "../../src/types/Obs.sol";
import {Holdings} from "../../src/types/Holdings.sol";
import {Bank, Cohort, RING} from "../../src/types/Bank.sol";

/// @notice Replays test/vectors/scenario.json against `VaultHarness.checkpoint` (the vault's `_checkpoint` with a
///         scripted observation and live read), bit for bit. This build's accrual step is inert, so the runner takes
///         the cases that mint no base (every step's `Checkpoint.base` is 0, `baseOwed` stays 0, nothing is deferred)
///         and compares every state field except `records`; the full runner comes with base accrual and records.
contract ScenarioVectorsTest is VectorBase, ScriptedVaultBase {
    /// @dev The number of no-base cases in the current file; a change in the generator must be looked at.
    uint256 internal constant RUNNABLE = 18;

    function test_U_vest_gate__scenarioVectorsWithoutBaseBitForBit() public {
        string[] memory cs = _cases("scenario");
        uint256 ran;
        for (uint256 i; i < cs.length; ++i) {
            if (_mintsNoBase(cs[i])) {
                _runCase(cs[i]);
                ++ran;
            }
        }
        assertEq(ran, RUNNABLE, "runnable no-base cases");
    }

    /// @notice U-base, F-6: every scenario case (base accrual, deferral into `baseOwed`, the breach's forfeit of
    ///         pre-breach and deferred base, the mint price), bit for bit against the model.
    function test_U_base__scenarioVectorsBitForBit() public {
        string[] memory cs = _cases("scenario");
        for (uint256 i; i < cs.length; ++i) {
            _runCase(cs[i]);
        }
        assertEq(cs.length, 60, "scenario cases");
    }

    function _mintsNoBase(string memory c) internal view returns (bool) {
        if (_u(c, ".in.state.baseOwed") != 0) return false;
        for (uint256 i; vm.keyExistsJson(c, _p(".out.steps[", i, "]")); ++i) {
            string memory st = _p(".out.steps[", i, "]");
            if (_u(c, string.concat(st, ".state.baseOwed")) != 0) return false;
            for (uint256 k; vm.keyExistsJson(c, string.concat(st, _p(".emitted[", k, "]"))); ++k) {
                string memory e = string.concat(st, _p(".emitted[", k, "]"));
                string memory name = vm.parseJsonKeys(c, e)[0];
                if (_eq(name, "CheckpointDeferred")) return false;
                if (_eq(name, "Checkpoint") && _u(c, string.concat(e, ".Checkpoint.base")) != 0) return false;
            }
        }
        return true;
    }

    function _runCase(string memory c) internal {
        _keys(c, "$", _l4("id", "fn", "in", "out"));
        cid = _s(c, ".id");
        assertEq(_s(c, ".fn"), "VaultHarness.checkpoint", cid);
        _keys(c, ".in", _l3("tier", "state", "events"));
        assertEq(_s(c, ".in.tier"), "TEST", cid);
        VaultHarness h = _newHarness();
        _writeState(h, c, ".in.state");
        for (uint256 i; vm.keyExistsJson(c, _p(".in.events[", i, "]")); ++i) {
            string memory ev = _p(".in.events[", i, "]");
            _keys(c, ev, _l4("block", "ts", "obs", "live"));
            vm.roll(_u(c, string.concat(ev, ".block")));
            vm.warp(_u(c, string.concat(ev, ".ts")));
            Obs memory o = _obs(c, string.concat(ev, ".obs"));
            string memory live = _s(c, string.concat(ev, ".live"));
            bool ok = !_eq(live, "fail");
            vm.recordLogs();
            h.checkpoint(o, ok, ok ? int24(vm.parseInt(live)) : int24(0));
            Vm.Log[] memory logs = vm.getRecordedLogs();
            string memory st = _p(".out.steps[", i, "]");
            _keys(c, st, _l3("state", "liveReads", "emitted"));
            assertEq(h.liveReads(), _u(c, string.concat(st, ".liveReads")), string.concat(cid, _p(" liveReads @", i, "")));
            _checkState(h, c, string.concat(st, ".state"));
            _checkEmitted(c, string.concat(st, ".emitted"), logs);
        }
    }

    // ------------------------------------------------------------------ state

    function _writeState(VaultHarness h, string memory c, string memory p) internal {
        _keys(c, p, _stateFields());
        string memory hp = string.concat(p, ".holdings");
        _keys(c, hp, _l5("idle0", "idle1", "L", "lo", "hi"));
        h.setHoldings(
            Holdings(
                uint128(_u(c, string.concat(hp, ".idle0"))),
                uint128(_u(c, string.concat(hp, ".idle1"))),
                uint128(_u(c, string.concat(hp, ".L"))),
                int24(_i(c, string.concat(hp, ".lo"))),
                int24(_i(c, string.concat(hp, ".hi")))
            )
        );
        h.setBank(_readBank(c, string.concat(p, ".bank")));
        h.setSupply(_u(c, string.concat(p, ".S")));
        h.setScalars(
            uint128(_u(c, string.concat(p, ".NAV0"))),
            uint64(_u(c, string.concat(p, ".delta0"))),
            uint64(_u(c, string.concat(p, ".tauV0"))),
            uint40(_u(c, string.concat(p, ".segStart"))),
            uint128(_u(c, string.concat(p, ".baseOwed"))),
            uint32(_u(c, string.concat(p, ".epoch"))),
            uint64(_u(c, string.concat(p, ".epochSwapFrac"))),
            uint16(_u(c, string.concat(p, ".swapCount")))
        );
        assertEq(vm.parseJsonKeys(c, string.concat(p, ".records")).length, 0, string.concat(cid, " in.state.records"));
    }

    function _checkState(VaultHarness h, string memory c, string memory p) internal view {
        _keys(c, p, _stateFields());
        Holdings memory hd = h.holdings();
        string memory hp = string.concat(p, ".holdings");
        assertEq(hd.idle0, _u(c, string.concat(hp, ".idle0")), string.concat(cid, " idle0"));
        assertEq(hd.idle1, _u(c, string.concat(hp, ".idle1")), string.concat(cid, " idle1"));
        assertEq(hd.L, _u(c, string.concat(hp, ".L")), string.concat(cid, " L"));
        assertEq(int256(hd.lo), _i(c, string.concat(hp, ".lo")), string.concat(cid, " lo"));
        assertEq(int256(hd.hi), _i(c, string.concat(hp, ".hi")), string.concat(cid, " hi"));
        _checkBank(h.bankFull(), c, string.concat(p, ".bank"));
        Scalars memory s = h.scalars();
        assertEq(s.nav0, _u(c, string.concat(p, ".NAV0")), string.concat(cid, " NAV0"));
        assertEq(s.delta0, _u(c, string.concat(p, ".delta0")), string.concat(cid, " delta0"));
        assertEq(s.tauV0, _u(c, string.concat(p, ".tauV0")), string.concat(cid, " tauV0"));
        assertEq(s.segStart, _u(c, string.concat(p, ".segStart")), string.concat(cid, " segStart"));
        assertEq(s.baseOwed, _u(c, string.concat(p, ".baseOwed")), string.concat(cid, " baseOwed"));
        assertEq(s.epoch, _u(c, string.concat(p, ".epoch")), string.concat(cid, " epoch"));
        assertEq(s.epochSwapFrac, _u(c, string.concat(p, ".epochSwapFrac")), string.concat(cid, " epochSwapFrac"));
        assertEq(s.swapCount, _u(c, string.concat(p, ".swapCount")), string.concat(cid, " swapCount"));
        assertEq(h.totalSupply(), _u(c, string.concat(p, ".S")), string.concat(cid, " S"));
    }

    function _readBank(string memory c, string memory p) internal view returns (Bank memory b) {
        _keys(c, p, _bankFields());
        for (uint256 k; k < RING; ++k) {
            string memory e = string.concat(p, _p(".cohorts[", k, "]"));
            _keys(c, e, _l2("epoch", "shares"));
            b.cohorts[k] = Cohort(uint32(_u(c, string.concat(e, ".epoch"))), uint128(_u(c, string.concat(e, ".shares"))));
        }
        assertFalse(vm.keyExistsJson(c, string.concat(p, _p(".cohorts[", RING, "]"))), "cohort ring size");
        b.maturedPending = uint128(_u(c, string.concat(p, ".maturedPending")));
        b.vestedUnclaimed = uint128(_u(c, string.concat(p, ".vestedUnclaimed")));
        b.bondShares = uint128(_u(c, string.concat(p, ".bondShares")));
        b.forfeitPool = uint128(_u(c, string.concat(p, ".forfeitPool")));
        b.forfeitTotal = uint128(_u(c, string.concat(p, ".forfeitTotal")));
        b.forfeitStart = uint40(_u(c, string.concat(p, ".forfeitStart")));
        b.dripBlocks = uint40(_u(c, string.concat(p, ".dripBlocks")));
        b.releaseDelay = uint40(_u(c, string.concat(p, ".releaseDelay")));
        b.breaches = uint32(_u(c, string.concat(p, ".breaches")));
        b.mode = uint8(_u(c, string.concat(p, ".mode")));
        b.placedByCurator = _b(c, string.concat(p, ".placedByCurator"));
        b.exitBlock = uint40(_u(c, string.concat(p, ".exitBlock")));
        b.reserveShares = uint128(_u(c, string.concat(p, ".reserveShares")));
        b.reserveBond = uint128(_u(c, string.concat(p, ".reserveBond")));
        b.reserveForfeit = _b(c, string.concat(p, ".reserveForfeit"));
    }

    function _checkBank(Bank memory got, string memory c, string memory p) internal view {
        Bank memory w = _readBank(c, p);
        for (uint256 k; k < RING; ++k) {
            assertEq(got.cohorts[k].epoch, w.cohorts[k].epoch, string.concat(cid, _p(" cohort ", k, " epoch")));
            assertEq(got.cohorts[k].shares, w.cohorts[k].shares, string.concat(cid, _p(" cohort ", k, " shares")));
        }
        assertEq(got.maturedPending, w.maturedPending, string.concat(cid, " maturedPending"));
        assertEq(got.vestedUnclaimed, w.vestedUnclaimed, string.concat(cid, " vestedUnclaimed"));
        assertEq(got.bondShares, w.bondShares, string.concat(cid, " bondShares"));
        assertEq(got.forfeitPool, w.forfeitPool, string.concat(cid, " forfeitPool"));
        assertEq(got.forfeitTotal, w.forfeitTotal, string.concat(cid, " forfeitTotal"));
        assertEq(got.forfeitStart, w.forfeitStart, string.concat(cid, " forfeitStart"));
        assertEq(got.dripBlocks, w.dripBlocks, string.concat(cid, " dripBlocks"));
        assertEq(got.releaseDelay, w.releaseDelay, string.concat(cid, " releaseDelay"));
        assertEq(got.breaches, w.breaches, string.concat(cid, " breaches"));
        assertEq(got.mode, w.mode, string.concat(cid, " mode"));
        assertEq(got.placedByCurator, w.placedByCurator, string.concat(cid, " placedByCurator"));
        assertEq(got.exitBlock, w.exitBlock, string.concat(cid, " exitBlock"));
        assertEq(got.reserveShares, w.reserveShares, string.concat(cid, " reserveShares"));
        assertEq(got.reserveBond, w.reserveBond, string.concat(cid, " reserveBond"));
        assertEq(got.reserveForfeit, w.reserveForfeit, string.concat(cid, " reserveForfeit"));
    }

    // ------------------------------------------------------------------ observation and events

    function _obs(string memory c, string memory p) internal view returns (Obs memory o) {
        string[] memory f = new string[](17);
        (f[0], f[1], f[2], f[3], f[4], f[5]) = ("med9", "tickSpot", "refTick", "refCachedAt", "refFresh", "resetStreak");
        (f[6], f[7], f[8], f[9], f[10], f[11]) = ("gapSinceVerify", "emptyRange", "tauV", "tauB", "outBlocks", "breachSeq");
        (f[12], f[13], f[14], f[15], f[16]) =
            ("pendingBreaches", "lastPendingBlock", "tauVAtLastPending", "gridIndex", "epoch");
        _keys(c, p, f);
        o.med9 = int24(_i(c, string.concat(p, ".med9")));
        o.tickSpot = int24(_i(c, string.concat(p, ".tickSpot")));
        o.refTick = int24(_i(c, string.concat(p, ".refTick")));
        o.refCachedAt = uint40(_u(c, string.concat(p, ".refCachedAt")));
        o.refFresh = _b(c, string.concat(p, ".refFresh"));
        o.resetStreak = uint8(_u(c, string.concat(p, ".resetStreak")));
        o.gapSinceVerify = _b(c, string.concat(p, ".gapSinceVerify"));
        o.emptyRange = _b(c, string.concat(p, ".emptyRange"));
        o.tauV = uint64(_u(c, string.concat(p, ".tauV")));
        o.tauB = uint64(_u(c, string.concat(p, ".tauB")));
        o.outBlocks = uint64(_u(c, string.concat(p, ".outBlocks")));
        o.breachSeq = uint32(_u(c, string.concat(p, ".breachSeq")));
        o.pendingBreaches = uint32(_u(c, string.concat(p, ".pendingBreaches")));
        o.lastPendingBlock = uint40(_u(c, string.concat(p, ".lastPendingBlock")));
        o.tauVAtLastPending = uint64(_u(c, string.concat(p, ".tauVAtLastPending")));
        o.gridIndex = uint32(_u(c, string.concat(p, ".gridIndex")));
        o.epoch = uint32(_u(c, string.concat(p, ".epoch")));
    }

    /// @dev The pinned events, in order: Checkpoint, BreachProcessed, CheckpointDeferred and the ackBreaches stub.
    function _checkEmitted(string memory c, string memory p, Vm.Log[] memory logs) internal view {
        uint256 j;
        for (uint256 k; vm.keyExistsJson(c, string.concat(p, _p("[", k, "]"))); ++k) {
            string memory e = string.concat(p, _p("[", k, "]"));
            string memory name = vm.parseJsonKeys(c, e)[0];
            while (j < logs.length && !_pinned(logs[j].topics[0])) ++j;
            assertLt(j, logs.length, string.concat(cid, " missing event ", name));
            Vm.Log memory l = logs[j++];
            if (_eq(name, "ackBreaches")) {
                assertEq(l.topics[0], T_ACK, string.concat(cid, " ackBreaches"));
                assertEq(abi.decode(l.data, (uint32)), _u(c, string.concat(e, ".ackBreaches.n")), cid);
            } else if (_eq(name, "BreachProcessed")) {
                assertEq(l.topics[0], T_BREACH, string.concat(cid, " BreachProcessed"));
                (uint32 n, uint40 last, uint256 forfeited) = abi.decode(l.data, (uint32, uint40, uint256));
                string memory q = string.concat(e, ".BreachProcessed");
                assertEq(n, _u(c, string.concat(q, ".count")), string.concat(cid, " count"));
                assertEq(last, _u(c, string.concat(q, ".lastBlock")), string.concat(cid, " lastBlock"));
                assertEq(forfeited, _u(c, string.concat(q, ".forfeited")), string.concat(cid, " forfeited"));
            } else if (_eq(name, "CheckpointDeferred")) {
                assertEq(l.topics[0], T_DEFERRED, string.concat(cid, " CheckpointDeferred"));
            } else {
                assertTrue(_eq(name, "Checkpoint"), string.concat(cid, " unexpected pinned event ", name));
                assertEq(l.topics[0], T_CHECKPOINT, string.concat(cid, " Checkpoint"));
                _checkCheckpoint(c, string.concat(e, ".Checkpoint"), l.data);
            }
        }
        while (j < logs.length && !_pinned(logs[j].topics[0])) ++j;
        assertEq(j, logs.length, string.concat(cid, " extra pinned event"));
    }

    function _checkCheckpoint(string memory c, string memory q, bytes memory data) internal view {
        CheckpointLog memory g = abi.decode(data, (CheckpointLog));
        assertEq(g.tauV, _u(c, string.concat(q, ".tauV")), string.concat(cid, " Checkpoint.tauV"));
        assertEq(g.tauB, _u(c, string.concat(q, ".tauB")), string.concat(cid, " Checkpoint.tauB"));
        assertEq(g.nav0, _u(c, string.concat(q, ".nav0")), string.concat(cid, " Checkpoint.nav0"));
        assertEq(g.delta, _u(c, string.concat(q, ".delta")), string.concat(cid, " Checkpoint.delta"));
        assertEq(g.base, _u(c, string.concat(q, ".base")), string.concat(cid, " Checkpoint.base"));
        assertEq(g.L, _u(c, string.concat(q, ".L")), string.concat(cid, " Checkpoint.L"));
        assertEq(int256(g.lo), _i(c, string.concat(q, ".lo")), string.concat(cid, " Checkpoint.lo"));
        assertEq(int256(g.hi), _i(c, string.concat(q, ".hi")), string.concat(cid, " Checkpoint.hi"));
    }

    function _pinned(bytes32 t) internal pure returns (bool) {
        return t == T_ACK || t == T_BREACH || t == T_CHECKPOINT || t == T_DEFERRED;
    }

    function _stateFields() internal pure returns (string[] memory f) {
        f = new string[](12);
        (f[0], f[1], f[2], f[3], f[4], f[5]) = ("holdings", "bank", "NAV0", "delta0", "tauV0", "segStart");
        (f[6], f[7], f[8], f[9], f[10], f[11]) = ("S", "baseOwed", "epoch", "epochSwapFrac", "swapCount", "records");
    }

    function _bankFields() internal pure returns (string[] memory f) {
        f = new string[](16);
        (f[0], f[1], f[2], f[3], f[4], f[5]) =
            ("cohorts", "maturedPending", "vestedUnclaimed", "bondShares", "forfeitPool", "forfeitTotal");
        (f[6], f[7], f[8], f[9], f[10], f[11]) =
            ("forfeitStart", "dripBlocks", "releaseDelay", "breaches", "mode", "placedByCurator");
        (f[12], f[13], f[14], f[15]) = ("exitBlock", "reserveShares", "reserveBond", "reserveForfeit");
    }
}
