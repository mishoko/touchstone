// SPDX-License-Identifier: MIT
pragma solidity 0.8.26;

import {Test, Vm} from "forge-std/Test.sol";
import {PoolManager} from "@uniswap/v4-core/src/PoolManager.sol";
import {Hooks} from "@uniswap/v4-core/src/libraries/Hooks.sol";
import {Currency} from "@uniswap/v4-core/src/types/Currency.sol";
import {TickMath} from "@uniswap/v4-core/src/libraries/TickMath.sol";
import {HookArgs} from "../../src/interfaces/ITouchstoneFactory.sol";
import {ITouchstoneHook} from "../../src/interfaces/ITouchstoneHook.sol";
import {TouchstoneHook} from "../../src/TouchstoneHook.sol";
import {Obs} from "../../src/types/Obs.sol";
import {Tier} from "../../src/types/Tier.sol";
import {Tiers} from "../../script/Tiers.sol";
import {HookHarness} from "../utils/HookHarness.sol";
import {MockRefPool} from "../utils/MockRefPool.sol";
import {JsonSlice} from "../utils/JsonSlice.sol";

/// @notice Replays every case of test/vectors/clock.json against HookHarness and asserts bit-for-bit equality of the
///         observation, the state, the frozen records written and the events emitted after every event. Any field the
///         runner does not know fails the run.
contract ClockVectorsTest is Test {
    uint160 internal constant FLAGS = Hooks.BEFORE_INITIALIZE_FLAG | Hooks.BEFORE_SWAP_FLAG | Hooks.BEFORE_DONATE_FLAG;
    address internal constant VAULT = address(0x5A17);
    address internal constant TOKEN0 = address(0x1000);
    address internal constant TOKEN1 = address(0x2000);
    uint256 internal constant CHUNK = 10;
    uint256 internal constant MAX_CASES = 60; // six chunks

    PoolManager internal manager;
    string internal cid; // the current case id, for messages

    function setUp() public {
        manager = new PoolManager(address(this));
    }

    function test_U_clock__goldenVectorsChunk0() public {
        _run(0);
    }

    function test_U_clock__goldenVectorsChunk1() public {
        _run(1);
    }

    function test_U_clock__goldenVectorsChunk2() public {
        _run(2);
    }

    function test_U_clock__goldenVectorsChunk3() public {
        _run(3);
    }

    function test_U_clock__goldenVectorsChunk4() public {
        _run(4);
    }

    function test_U_clock__goldenVectorsChunk5() public {
        _run(5);
    }

    // ------------------------------------------------------------------ file and case structure

    function _run(uint256 chunk) internal {
        string memory json = vm.readFile("test/vectors/clock.json");
        _keys(json, "$", _list4("schema", "modelHash", "conventions", "cases"));
        assertEq(vm.parseJsonString(json, ".schema"), "touchstone-vectors/1", "schema");
        string[] memory cs = JsonSlice.elements(json, "cases");
        assertGt(cs.length, 0, "no cases");
        assertLe(cs.length, MAX_CASES, "more cases than the runner's chunks cover");
        uint256 end = (chunk + 1) * CHUNK < cs.length ? (chunk + 1) * CHUNK : cs.length;
        for (uint256 i = chunk * CHUNK; i < end; ++i) {
            _runCase(cs[i], i);
        }
    }

    function _runCase(string memory c, uint256 idx) internal {
        _keys(c, "$", _list4("id", "fn", "in", "out"));
        cid = vm.parseJsonString(c, ".id");
        assertEq(vm.parseJsonString(c, ".fn"), "HookHarness.events", cid);
        _keys(c, ".in", _list3("tier", "witness", "events"));
        _keys(c, ".in.witness", _list2("lo", "hi"));
        _keys(c, ".out", _list1("steps"));

        // fresh hook and reference per case, at the first event's block and time
        (MockRefPool ref, HookHarness h) = _deploy(c, idx);
        h.setWitnessRange(int24(_i(c, ".in.witness.lo")), int24(_i(c, ".in.witness.hi")));

        uint256 j;
        for (; vm.keyExistsJson(c, _p(".in.events[", j, "]")); ++j) {
            // Every cheatcode call copies the case string into memory; nothing allocated by one event is used by the
            // next, so the free-memory pointer is reset after each event to keep memory bounded.
            uint256 fmp;
            assembly ("memory-safe") {
                fmp := mload(0x40)
            }
            _runEvent(c, j, ref, h);
            assembly ("memory-safe") {
                mstore(0x40, fmp)
            }
        }
        assertGt(j, 0, cid);
        assertFalse(vm.keyExistsJson(c, _p(".out.steps[", j, "]")), "more steps than events");
    }

    function _deploy(string memory c, uint256 idx) internal returns (MockRefPool ref, HookHarness h) {
        string memory tierName = vm.parseJsonString(c, ".in.tier");
        Tier memory t;
        if (keccak256(bytes(tierName)) == keccak256("TEST")) (t,) = Tiers.testing();
        else if (keccak256(bytes(tierName)) == keccak256("DEMO")) (t,) = Tiers.demo();
        else revert(string.concat("unknown tier ", tierName));
        string memory op = _op(c, 0);
        string memory e0 = string.concat(".in.events[0].", op);
        vm.roll(_u(c, string.concat(e0, ".block")));
        vm.warp(_u(c, string.concat(e0, ".ts")));
        ref = new MockRefPool(TOKEN0, TOKEN1, 500, 0, 0);
        t.REF_POOL = address(ref);
        HookArgs memory a = HookArgs({
            poolManager: address(manager),
            vault: VAULT,
            currency0: Currency.wrap(TOKEN0),
            currency1: Currency.wrap(TOKEN1),
            fee: 500,
            tickSpacing: 10,
            seedSqrtPriceX96: TickMath.getSqrtPriceAtTick(0),
            tier: t
        });
        address at = address(uint160((idx + 1) << 100) | FLAGS);
        deployCodeTo("HookHarness.sol:HookHarness", abi.encode(a), at);
        h = HookHarness(at);
    }

    // ------------------------------------------------------------------ one event

    function _runEvent(string memory c, uint256 j, MockRefPool ref, HookHarness h) internal {
        string memory op = _op(c, j);
        string memory e = string.concat(_p(".in.events[", j, "]."), op);
        bytes32 k = keccak256(bytes(op));
        bool isRange = k == keccak256("setVaultRange");
        bool isAck = k == keccak256("ackBreaches");
        {
            string[] memory f = new string[](isRange ? 8 : isAck ? 6 : 5);
            (f[0], f[1], f[2], f[3], f[4]) = ("block", "ts", "sqrtPrice", "tick", "ref");
            if (isRange) (f[5], f[6], f[7]) = ("lo", "hi", "liveTick");
            if (isAck) f[5] = "n";
            _keys(c, e, f);
        }
        vm.roll(_u(c, string.concat(e, ".block")));
        vm.warp(_u(c, string.concat(e, ".ts")));
        h.setSlot0(uint160(_u(c, string.concat(e, ".sqrtPrice"))), int24(_i(c, string.concat(e, ".tick"))));
        string memory r = vm.parseJsonString(c, string.concat(e, ".ref"));
        if (keccak256(bytes(r)) == keccak256("fail")) {
            ref.setMode(MockRefPool.Mode.Revert);
        } else {
            ref.setMode(MockRefPool.Mode.Ok);
            uint32[] memory starts = new uint32[](1);
            int24[] memory ticks = new int24[](1);
            ticks[0] = int24(vm.parseInt(r));
            ref.setPath(starts, ticks);
        }

        bytes memory data;
        bool asVault = true;
        if (k == keccak256("snapshot")) {
            data = abi.encodeCall(TouchstoneHook.snapshotIfNewBlock, ());
            asVault = false;
        } else if (k == keccak256("refreshRef")) {
            data = abi.encodeCall(TouchstoneHook.refreshRef, ());
            asVault = false;
        } else if (k == keccak256("refresh")) {
            data = abi.encodeCall(HookHarness.exposedRefresh, ());
            asVault = false;
        } else if (k == keccak256("arm")) {
            data = abi.encodeCall(TouchstoneHook.armAtSeed, ());
        } else if (isRange) {
            data = abi.encodeCall(
                TouchstoneHook.setVaultRange,
                (
                    int24(_i(c, string.concat(e, ".lo"))),
                    int24(_i(c, string.concat(e, ".hi"))),
                    int24(_i(c, string.concat(e, ".liveTick")))
                )
            );
        } else if (isAck) {
            data = abi.encodeCall(TouchstoneHook.ackBreaches, (uint32(_u(c, string.concat(e, ".n")))));
        } else {
            revert(string.concat("unknown op ", op));
        }

        HookHarness.Frozen[8] memory before = _frozenAll(h);
        bytes memory stateBefore = abi.encode(h.obs(), _state(h));
        vm.recordLogs();
        if (asVault) vm.prank(VAULT);
        (bool ok, bytes memory ret) = address(h).call(data);
        Vm.Log[] memory logs = vm.getRecordedLogs();

        string memory s = _p(".out.steps[", j, "]");
        string memory at = string.concat(cid, " step ", vm.toString(j), " ", op);
        if (vm.keyExistsJson(c, string.concat(s, ".revert"))) {
            _keys(c, s, _list1("revert"));
            assertFalse(ok, string.concat(at, ": expected a revert"));
            assertEq(bytes4(ret), _errorSelector(vm.parseJsonString(c, string.concat(s, ".revert"))), at);
            assertEq(abi.encode(h.obs(), _state(h)), stateBefore, string.concat(at, ": state changed"));
            return;
        }
        assertTrue(ok, string.concat(at, ": unexpected revert"));
        _keys(c, s, _list4("obs", "state", "frozen", "emitted"));
        _checkObs(c, string.concat(s, ".obs"), h.obs(), at);
        _checkState(c, string.concat(s, ".state"), h, at);
        _checkFrozen(c, string.concat(s, ".frozen"), before, _frozenAll(h), at);
        _checkEmitted(c, string.concat(s, ".emitted"), logs, address(h), at);
    }

    // ------------------------------------------------------------------ checks

    function _checkObs(string memory c, string memory p, Obs memory o, string memory at) internal view {
        string[] memory f = new string[](17);
        (f[0], f[1], f[2], f[3], f[4], f[5]) =
            ("med9", "tickSpot", "refTick", "refCachedAt", "refFresh", "resetStreak");
        (f[6], f[7], f[8], f[9], f[10], f[11]) =
            ("gapSinceVerify", "emptyRange", "tauV", "tauB", "outBlocks", "breachSeq");
        (f[12], f[13], f[14], f[15], f[16]) =
            ("pendingBreaches", "lastPendingBlock", "tauVAtLastPending", "gridIndex", "epoch");
        _keys(c, p, f);
        int256[17] memory v = [
            int256(o.med9),
            o.tickSpot,
            o.refTick,
            int256(uint256(o.refCachedAt)),
            o.refFresh ? int256(1) : int256(0),
            int256(uint256(o.resetStreak)),
            o.gapSinceVerify ? int256(1) : int256(0),
            o.emptyRange ? int256(1) : int256(0),
            int256(uint256(o.tauV)),
            int256(uint256(o.tauB)),
            int256(uint256(o.outBlocks)),
            int256(uint256(o.breachSeq)),
            int256(uint256(o.pendingBreaches)),
            int256(uint256(o.lastPendingBlock)),
            int256(uint256(o.tauVAtLastPending)),
            int256(uint256(o.gridIndex)),
            int256(uint256(o.epoch))
        ];
        for (uint256 i; i < 17; ++i) {
            string memory q = string.concat(p, ".", f[i]);
            int256 want = (i == 4 || i == 6 || i == 7)
                ? (vm.parseJsonBool(c, q) ? int256(1) : int256(0))
                : vm.parseInt(vm.parseJsonString(c, q));
            assertEq(v[i], want, string.concat(at, ": obs.", f[i]));
        }
    }

    function _checkState(string memory c, string memory p, HookHarness h, string memory at) internal view {
        string[] memory f = new string[](6);
        (f[0], f[1], f[2], f[3], f[4], f[5]) = ("refGrid", "lastSnap", "armed", "vaultLo", "vaultHi", "epochStartRef");
        _keys(c, p, f);
        (uint32 refGrid, uint40 lastSnap, bool armed, int24 lo, int24 hi, int24 esr) = h.state();
        assertEq(uint256(refGrid), _u(c, string.concat(p, ".refGrid")), string.concat(at, ": state.refGrid"));
        assertEq(uint256(lastSnap), _u(c, string.concat(p, ".lastSnap")), string.concat(at, ": state.lastSnap"));
        assertEq(armed, vm.parseJsonBool(c, string.concat(p, ".armed")), string.concat(at, ": state.armed"));
        assertEq(int256(lo), _i(c, string.concat(p, ".vaultLo")), string.concat(at, ": state.vaultLo"));
        assertEq(int256(hi), _i(c, string.concat(p, ".vaultHi")), string.concat(at, ": state.vaultHi"));
        assertEq(int256(esr), _i(c, string.concat(p, ".epochStartRef")), string.concat(at, ": state.epochStartRef"));
    }

    /// @dev The entries written by the event are the slots that changed (grid indices only grow, so every write
    ///      changes its slot), in ascending grid order.
    function _checkFrozen(
        string memory c,
        string memory p,
        HookHarness.Frozen[8] memory a,
        HookHarness.Frozen[8] memory b,
        string memory at
    ) internal view {
        HookHarness.Frozen[] memory w = new HookHarness.Frozen[](8);
        uint256 n;
        for (uint256 i; i < 8; ++i) {
            if (keccak256(abi.encode(a[i])) != keccak256(abi.encode(b[i]))) w[n++] = b[i];
        }
        for (uint256 i = 1; i < n; ++i) {
            for (uint256 x = i; x > 0 && w[x - 1].gridIndex > w[x].gridIndex; --x) {
                (w[x - 1], w[x]) = (w[x], w[x - 1]);
            }
        }
        uint256 m;
        for (; vm.keyExistsJson(c, _p(string.concat(p, "["), m, "]")); ++m) {
            string memory q = _p(string.concat(p, "["), m, "]");
            _keys(c, q, _list4("gridIndex", "med9", "tauV", "tauB"));
            assertLt(m, n, string.concat(at, ": fewer frozen writes than expected"));
            assertEq(uint256(w[m].gridIndex), _u(c, string.concat(q, ".gridIndex")), string.concat(at, ": frozen.grid"));
            assertEq(int256(w[m].med9), _i(c, string.concat(q, ".med9")), string.concat(at, ": frozen.med9"));
            assertEq(uint256(w[m].tauV), _u(c, string.concat(q, ".tauV")), string.concat(at, ": frozen.tauV"));
            assertEq(uint256(w[m].tauB), _u(c, string.concat(q, ".tauB")), string.concat(at, ": frozen.tauB"));
        }
        assertEq(n, m, string.concat(at, ": frozen write count"));
    }

    function _checkEmitted(string memory c, string memory p, Vm.Log[] memory logs, address h, string memory at)
        internal
        view
    {
        uint256 li;
        uint256 m;
        for (; vm.keyExistsJson(c, _p(string.concat(p, "["), m, "]")); ++m) {
            string memory q = _p(string.concat(p, "["), m, "]");
            string[] memory names = vm.parseJsonKeys(c, q);
            assertEq(names.length, 1, string.concat(at, ": emitted entry"));
            string memory name = names[0];
            q = string.concat(q, ".", name);
            (bytes32 topic, string[] memory f) = _eventShape(name);
            _keys(c, q, f);
            bytes memory data;
            for (uint256 i; i < f.length; ++i) {
                data = bytes.concat(data, abi.encode(vm.parseInt(vm.parseJsonString(c, string.concat(q, ".", f[i])))));
            }
            while (li < logs.length && logs[li].emitter != h) ++li;
            assertLt(li, logs.length, string.concat(at, ": missing event ", name));
            assertEq(logs[li].topics[0], topic, string.concat(at, ": event ", name));
            assertEq(logs[li].data, data, string.concat(at, ": event data ", name));
            ++li;
        }
        while (li < logs.length && logs[li].emitter != h) ++li;
        assertEq(li, logs.length, string.concat(at, ": unexpected extra event"));
    }

    function _eventShape(string memory name) internal pure returns (bytes32 topic, string[] memory f) {
        bytes32 k = keccak256(bytes(name));
        if (k == keccak256("RefRefreshed")) return (ITouchstoneHook.RefRefreshed.selector, _list2("grid", "tick"));
        if (k == keccak256("RefReadFailed")) return (ITouchstoneHook.RefReadFailed.selector, _list1("grid"));
        if (k == keccak256("GapCleared")) return (ITouchstoneHook.GapCleared.selector, _list1("kind"));
        if (k == keccak256("GapSet")) return (ITouchstoneHook.GapSet.selector, _list1("block"));
        if (k == keccak256("BreachLatched")) return (ITouchstoneHook.BreachLatched.selector, _list2("seq", "block"));
        if (k == keccak256("RangeRegistered")) {
            return (ITouchstoneHook.RangeRegistered.selector, _list3("lo", "hi", "liveTick"));
        }
        revert(string.concat("unknown event ", name));
    }

    function _errorSelector(string memory name) internal pure returns (bytes4) {
        bytes32 k = keccak256(bytes(name));
        if (k == keccak256("RefUnreadable")) return ITouchstoneHook.RefUnreadable.selector;
        if (k == keccak256("OnlyVault")) return ITouchstoneHook.OnlyVault.selector;
        if (k == keccak256("RefGas")) return ITouchstoneHook.RefGas.selector;
        if (k == keccak256("Armed")) return ITouchstoneHook.Armed.selector;
        revert(string.concat("unknown error ", name));
    }

    // ------------------------------------------------------------------ helpers

    function _state(HookHarness h) internal view returns (bytes memory) {
        (uint32 a, uint40 b, bool c, int24 d, int24 e, int24 f) = h.state();
        return abi.encode(a, b, c, d, e, f);
    }

    function _frozenAll(HookHarness h) internal view returns (HookHarness.Frozen[8] memory f) {
        for (uint256 i; i < 8; ++i) {
            f[i] = h.frozenSlot(i);
        }
    }

    function _op(string memory c, uint256 j) internal pure returns (string memory) {
        string[] memory k = vm.parseJsonKeys(c, _p(".in.events[", j, "]"));
        assertEq(k.length, 1, "an event has exactly one op");
        return k[0];
    }

    /// @dev Fails unless the object at `path` has exactly the keys `want`.
    function _keys(string memory c, string memory path, string[] memory want) internal view {
        string[] memory got = vm.parseJsonKeys(c, path);
        for (uint256 i; i < got.length; ++i) {
            bool known;
            for (uint256 x; x < want.length; ++x) {
                if (keccak256(bytes(got[i])) == keccak256(bytes(want[x]))) known = true;
            }
            assertTrue(known, string.concat(cid, ": unknown field ", got[i], " at ", path));
        }
        assertEq(got.length, want.length, string.concat(cid, ": missing field at ", path));
    }

    function _u(string memory c, string memory path) internal pure returns (uint256) {
        return vm.parseUint(vm.parseJsonString(c, path));
    }

    function _i(string memory c, string memory path) internal pure returns (int256) {
        return vm.parseInt(vm.parseJsonString(c, path));
    }

    function _p(string memory a, uint256 i, string memory b) internal pure returns (string memory) {
        return string.concat(a, vm.toString(i), b);
    }

    function _list1(string memory a) internal pure returns (string[] memory l) {
        l = new string[](1);
        l[0] = a;
    }

    function _list2(string memory a, string memory b) internal pure returns (string[] memory l) {
        l = new string[](2);
        (l[0], l[1]) = (a, b);
    }

    function _list3(string memory a, string memory b, string memory c) internal pure returns (string[] memory l) {
        l = new string[](3);
        (l[0], l[1], l[2]) = (a, b, c);
    }

    function _list4(string memory a, string memory b, string memory c, string memory d)
        internal
        pure
        returns (string[] memory l)
    {
        l = new string[](4);
        (l[0], l[1], l[2], l[3]) = (a, b, c, d);
    }
}
