// SPDX-License-Identifier: MIT
pragma solidity 0.8.26;

import {TickMath} from "@uniswap/v4-core/src/libraries/TickMath.sol";
import {Obs} from "../../src/types/Obs.sol";
import {HookBase} from "../utils/HookBase.sol";
import {HookModel} from "../utils/HookModel.sol";
import {MockRefPool} from "../utils/MockRefPool.sol";

/// @notice Differential fuzzing: random sequences of swaps (first-touch snapshots), refreshes, block rolls, reference
///         moves and failures, pool moves (including prices exactly on a range edge), registrations and breach
///         acknowledgements run on the hook and on a port of the reference model; every observable must agree after
///         every step.
abstract contract ModelTraceBase is HookBase {
    HookModel internal m;
    uint160 internal sqrtP;
    int24 internal tickP;

    function _setUpTrace(bool demo) internal {
        _setUpHook(demo);
        refConst(0);
        m = new HookModel(tier);
        _pool(0);
    }

    function _pool(int24 t) internal {
        _poolSqrt(TickMath.getSqrtPriceAtTick(t));
    }

    function _poolSqrt(uint160 s) internal {
        sqrtP = s;
        tickP = TickMath.getTickAtSqrtPrice(s);
        h.setSlot0(sqrtP, tickP);
    }

    function _ref(int24 t, bool fails) internal {
        if (fails) refMode(MockRefPool.Mode.Revert);
        else refConst(t);
        m.setRef(t, fails);
    }

    function _trace(uint256 seed, uint256 steps) internal {
        h.snapshotIfNewBlock();
        m.snapshot(sqrtP, tickP);
        vm.prank(VAULT);
        h.armAtSeed();
        m.armAtSeed();
        _compare();
        int24 lo;
        for (uint256 i; i < steps; ++i) {
            uint256 r = uint256(keccak256(abi.encode(seed, i)));
            uint256 op = r % 100;
            r >>= 8;
            if (op < 30) {
                advance(1 + r % 30);
                h.snapshotIfNewBlock();
                m.snapshot(sqrtP, tickP);
            } else if (op < 50) {
                advance(r % 4 == 0 ? 1 + (r >> 4) % 600 : 1 + (r >> 4) % 2 * grid());
                h.refreshRef();
                m.refreshRef(sqrtP, tickP);
            } else if (op < 58) {
                int24 t = r % 5 == 0 ? int24(5000) : int24(int256(r >> 4) % 3000);
                _ref(t, r % 7 == 0);
            } else if (op < 70) {
                uint256 kind = r % 4;
                if (kind == 0 && lo != 0) _poolSqrt(TickMath.getSqrtPriceAtTick(lo));
                else if (kind == 1 && lo != 0) _poolSqrt(TickMath.getSqrtPriceAtTick(lo + 2000));
                else _pool(int24(int256(r >> 4) % 2500));
            } else if (op < 76) {
                lo = int24(int256((r >> 4) % 200) * 10) - 2000;
                int24 live = lo + int24(int256((r >> 12) % 2000));
                h.snapshotIfNewBlock();
                m.snapshot(sqrtP, tickP);
                vm.prank(VAULT);
                h.setVaultRange(lo, lo + 2000, live);
                m.setVaultRange(lo, lo + 2000, live);
            } else if (op < 80) {
                vm.prank(VAULT);
                h.ackBreaches(uint32(r % 4));
                m.ackBreaches(r % 4);
            } else if (op < 86) {
                h.exposedRefresh();
                m.refresh();
            } else if (op < 90) {
                int24 wl = int24(int256(r % 300) * 10) - 1500;
                h.setWitnessRange(wl, wl + 2000);
                m.setWitness(wl, wl + 2000);
            } else {
                h.snapshotIfNewBlock(); // a second touch in the same block
                m.snapshot(sqrtP, tickP);
            }
            _compare();
        }
    }

    function _compare() internal view {
        Obs memory o = h.obs();
        assertEq(o.med9, m.med9(), "med9");
        assertEq(o.tickSpot, tickP, "tickSpot");
        assertEq(o.refTick, m.refTick(), "refTick");
        assertEq(o.refCachedAt, m.refCachedAt(), "refCachedAt");
        assertEq(o.refFresh, vm.getBlockNumber() < m.refCachedAt() + 2 * m.GRID(), "refFresh");
        assertEq(o.resetStreak, m.resetStreak(), "resetStreak");
        assertEq(o.gapSinceVerify, m.gapSinceVerify(), "gap");
        assertEq(o.emptyRange, m.emptyRange(), "emptyRange");
        assertEq(o.tauV, m.tauV(), "tauV");
        assertEq(o.tauB, m.tauB(), "tauB");
        assertEq(o.outBlocks, m.outBlocks(), "outBlocks");
        assertEq(o.breachSeq, m.breachSeq(), "breachSeq");
        assertEq(o.pendingBreaches, m.pendingBreaches(), "pending");
        assertEq(o.lastPendingBlock, m.lastPendingBlock(), "lastPendingBlock");
        assertEq(o.tauVAtLastPending, m.tauVAtLastPending(), "tauVAtLastPending");
        (uint32 refGrid, uint40 lastSnap, bool armed, int24 lo, int24 hi, int24 esr) = h.state();
        assertEq(refGrid, m.refGrid(), "refGrid");
        assertEq(lastSnap, m.lastSnap(), "lastSnap");
        assertEq(armed, m.armed(), "armed");
        assertEq(lo, m.vaultLo(), "vaultLo");
        assertEq(hi, m.vaultHi(), "vaultHi");
        assertEq(esr, m.epochStartRef(), "epochStartRef");
        uint256[] memory w = m.written();
        for (uint256 i; i < w.length; ++i) {
            (int24 med, uint64 tv, uint64 tb, bool ex) = h.frozen(uint32(w[i]));
            (int24 mm, uint256 mtv, uint256 mtb,) = m.frozen(w[i]);
            assertTrue(ex, "frozen exists");
            assertEq(med, mm, "frozen med9");
            assertEq(tv, mtv, "frozen tauV");
            assertEq(tb, mtb, "frozen tauB");
        }
    }
}

contract ModelTraceTest is ModelTraceBase {
    function setUp() public {
        _setUpTrace(false);
    }

    function test_F_7__modelAndHookTracesAreEqual(uint256 seed) public {
        _trace(seed, 80);
    }
}

contract ModelTraceDemoTest is ModelTraceBase {
    function setUp() public {
        _setUpTrace(true);
        vm.warp(vm.getBlockTimestamp() + 150); // a timestamp phase of 150 s
    }

    function test_F_7__modelAndHookTracesAreEqualDemoPhase150(uint256 seed) public {
        _trace(seed, 80);
    }
}
