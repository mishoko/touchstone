// SPDX-License-Identifier: MIT
pragma solidity 0.8.26;

import {Vm} from "forge-std/Vm.sol";
import {StateLibrary} from "@uniswap/v4-core/src/libraries/StateLibrary.sol";
import {TickMath} from "@uniswap/v4-core/src/libraries/TickMath.sol";
import {BalanceDelta} from "@uniswap/v4-core/src/types/BalanceDelta.sol";
import {ModifyLiquidityParams, SwapParams} from "@uniswap/v4-core/src/types/PoolOperation.sol";
import {PoolSwapTest} from "@uniswap/v4-core/src/test/PoolSwapTest.sol";
import {LiquidPoolBase} from "../utils/LiquidPoolBase.sol";
import {Sybil} from "../manipulation/CellBase.sol";
import {IPoolManager} from "@uniswap/v4-core/src/interfaces/IPoolManager.sol";
import {ScriptedVaultBase} from "../utils/ScriptedVault.sol";
import {VaultHarness, CheckpointLog} from "../utils/VaultHarness.sol";
import {LiquidityValue} from "../../src/libraries/LiquidityValue.sol";
import {ExecLedger} from "../../src/libraries/ExecLedger.sol";
import {Ladder} from "../../src/libraries/Ladder.sol";
import {Holdings} from "../../src/types/Holdings.sol";
import {Bank} from "../../src/types/Bank.sol";
import {Obs} from "../../src/types/Obs.sol";

/// @notice F-1 (spec §9.1): `LiquidityValue.amounts` equals what v4's `modifyLiquidity` pays out on a removal, and F-5:
///         a call with no curator action carries no charge. On the factory-deployed pool with third-party liquidity.
contract LiquidityValueFuzzTest is LiquidPoolBase {
    using StateLibrary for *;

    bytes32 internal constant T_ACTION = keccak256("Action(uint8,uint256,uint160,uint256,uint256)");

    function _pushTo(int24 t) internal {
        (uint160 sp,,,) = manager.getSlot0(poolId);
        uint160 target = TickMath.getSqrtPriceAtTick(t);
        if (sp == target) return;
        swapRouter.swap(
            key,
            SwapParams({zeroForOne: target < sp, amountSpecified: -int256(1e30), sqrtPriceLimitX96: target}),
            PoolSwapTest.TestSettings({takeClaims: false, settleUsingBurn: false}),
            ""
        );
    }

    /// @notice F-1: for any spot, aligned range and liquidity, a removal pays exactly `LiquidityValue.amounts` (both
    ///         rounded down), and the matching add costs at most 1 more of each token (v4 rounds the add up).
    function test_F_1__liquidityValueEqualsModifyLiquidityPayout(int24 t, int24 lo, uint16 w, uint128 L) public {
        t = int24(bound(t, -5000, 5000));
        int24 sp = TICK_SPACING;
        lo = int24(bound(lo, -8000, 7000)) / sp * sp;
        int24 hi = lo + sp * int24(uint24(1 + uint256(w) % 300));
        L = uint128(bound(L, 1, 1e24));
        _pushTo(t);
        (uint160 s,,,) = manager.getSlot0(poolId);
        (uint256 e0, uint256 e1) = LiquidityValue.amounts(L, lo, hi, s);
        // an LP account that settles its own deltas (v4's test router asserts non-zero amounts, which tiny L violates)
        Sybil lp = new Sybil(IPoolManager(address(manager)), key);
        token0.mint(address(lp), 1e30);
        token1.mint(address(lp), 1e30);
        uint256 b0 = token0.balanceOf(address(lp));
        uint256 b1 = token1.balanceOf(address(lp));
        lp.lp(lo, hi, int256(uint256(L)));
        uint256 a0 = b0 - token0.balanceOf(address(lp));
        uint256 a1 = b1 - token1.balanceOf(address(lp));
        (b0, b1) = (token0.balanceOf(address(lp)), token1.balanceOf(address(lp)));
        lp.lp(lo, hi, -int256(uint256(L)));
        assertEq(token0.balanceOf(address(lp)) - b0, e0, "removal amount0");
        assertEq(token1.balanceOf(address(lp)) - b1, e1, "removal amount1");
        assertTrue(a0 >= e0 && a0 <= e0 + 1, "add amount0 within 1 of the payout");
        assertTrue(a1 >= e1 && a1 <= e1 + 1, "add amount1 within 1 of the payout");
    }

    /// @notice F-5 (ledger): with no action (the holdings after equal the holdings before) the charge, its burn and its
    ///         gate are 0 at every spot, pre-move spot and reference, for idle balances and positions alike.
    function test_F_5__noActionMeasuresZero(
        uint96 i0,
        uint96 i1,
        uint96 L,
        int24 lo,
        int24 t0,
        int24 t1,
        int24 tr,
        uint64 reserve
    ) public pure {
        lo = int24(bound(lo, -100_000, 100_000)) / 10 * 10;
        Holdings memory h = Holdings(i0, i1, L, lo, lo + 1200);
        t0 = int24(bound(t0, -200_000, 200_000));
        t1 = int24(bound(t1, -200_000, 200_000));
        tr = int24(bound(tr, -200_000, 200_000));
        (uint256 charge,,, uint256 burn,,) = ExecLedger.measure(
            h,
            h,
            TickMath.getSqrtPriceAtTick(t0),
            TickMath.getSqrtPriceAtTick(t1),
            TickMath.getSqrtPriceAtTick(tr),
            reserve,
            1e24,
            0,
            0
        );
        assertEq(charge, 0, "no action, no charge");
        assertEq(burn, 0, "no burn");
    }

    /// @notice F-5 (vault): third-party swaps and time, then a checkpoint: no `Action` is emitted, the bond and the
    ///         reserve are untouched and nothing is pending (only base, which needs no action, may mint).
    function test_F_5__noActionChargesNothingOnTheVault(uint8 n, uint64 seed) public {
        hv.placeForTest(-1800, 200);
        Bank memory b0 = hv.bankFull();
        n = uint8(bound(n, 1, 8));
        vm.recordLogs();
        for (uint256 i; i < n; ++i) {
            seed = uint64(uint256(keccak256(abi.encode(seed, i))));
            _swap(seed % 2 == 0, 1e15 + seed % 1e18, 50);
            advance(1 + seed % 30);
        }
        hv.settle();
        Vm.Log[] memory logs = vm.getRecordedLogs();
        for (uint256 i; i < logs.length; ++i) {
            assertTrue(logs[i].topics.length == 0 || logs[i].topics[0] != T_ACTION, "no Action");
        }
        Bank memory b1 = hv.bankFull();
        assertEq(b1.bondShares, b0.bondShares, "bond untouched");
        assertEq(b1.reserveShares, 0, "no reserve");
        (uint40 tA,,,,) = hv.pendingAction();
        assertEq(tA, 0, "nothing pending");
    }
}

/// @notice F-6 (spec §3.5): the base amount is independent of how the observed time is segmented, up to 1 wei per
///         segment from the floors, with NAV₀ fixed between flows. Two scripted harnesses over the same Δτ_v: one
///         checkpoint against up to five; deferred (no price: the amount lands in `baseOwed` whole) and minted (the
///         `Checkpoint` events' base summed).
contract BaseSegmentationFuzzTest is ScriptedVaultBase {
    function _h(uint128 nav0, uint64 delta) internal returns (VaultHarness h) {
        h = _placedHarness();
        Bank memory b = h.bankFull();
        (b.placedByCurator, b.mode) = (true, Ladder.NORMAL);
        h.setBank(b);
        h.setScalars(nav0, delta, 0, 1150, 0, 10, 0, 0);
    }

    function _cp(VaultHarness h, uint64 tauV, bool priced) internal returns (uint256 base) {
        Obs memory o = _obsIn();
        o.tauV = tauV;
        o.refFresh = priced;
        vm.recordLogs();
        h.checkpoint(o, priced, 0);
        Vm.Log[] memory logs = vm.getRecordedLogs();
        CheckpointLog memory g = abi.decode(logs[logs.length - 1].data, (CheckpointLog));
        return g.base;
    }

    function test_F_6__baseAmountIndependentOfSegmentation(
        uint64 delta,
        uint64 nav0,
        uint32 total,
        uint32[4] memory cuts,
        bool priced
    ) public {
        delta = uint64(bound(delta, 1e15, 1e18));
        nav0 = uint64(bound(nav0, 1e15, 1e18)); // NAV' at tick 0 is 2e18: a priced mint stays below NAV'
        total = uint32(bound(total, 5, 1e6));
        VaultHarness one = _h(nav0, delta);
        VaultHarness many = _h(nav0, delta);
        uint256 whole = _cp(one, total, priced);
        uint256 sum;
        uint64 last;
        uint256 segs;
        for (uint256 i; i < 4; ++i) {
            uint64 c = uint64(bound(cuts[i], last, total));
            if (c == last) continue;
            sum += _cp(many, c, priced);
            last = c;
            ++segs;
        }
        sum += _cp(many, total, priced);
        ++segs;
        assertEq(many.scalars().nav0, nav0, "NAV0 fixed between flows");
        assertLe(sum, whole, "the floors only lose");
        assertLe(whole - sum, segs, "at most 1 wei per segment");
        if (!priced) {
            assertEq(one.scalars().baseOwed, whole, "deferred whole");
            assertEq(many.scalars().baseOwed, sum, "deferred per segment");
        }
        assertGt(whole, 0, "non-vacuous");
    }
}
