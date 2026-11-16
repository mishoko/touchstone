// SPDX-License-Identifier: MIT
pragma solidity 0.8.26;

import {Test} from "forge-std/Test.sol";
import {IPoolManager} from "@uniswap/v4-core/src/interfaces/IPoolManager.sol";
import {PoolKey} from "@uniswap/v4-core/src/types/PoolKey.sol";
import {PoolId} from "@uniswap/v4-core/src/types/PoolId.sol";
import {StateLibrary} from "@uniswap/v4-core/src/libraries/StateLibrary.sol";
import {TickMath} from "@uniswap/v4-core/src/libraries/TickMath.sol";
import {FullMath} from "@uniswap/v4-core/src/libraries/FullMath.sol";
import {SwapParams} from "@uniswap/v4-core/src/types/PoolOperation.sol";
import {PoolSwapTest} from "@uniswap/v4-core/src/test/PoolSwapTest.sol";
import {IERC20Errors} from "@openzeppelin/contracts/interfaces/draft-IERC6093.sol";
import {VaultHarness} from "../../utils/VaultHarness.sol";
import {MockRefPool} from "../../utils/MockRefPool.sol";
import {MockToken} from "../../utils/Stubs.sol";
import {ExpectedFlows} from "../../utils/ExpectedFlows.sol";
import {ITouchstoneHook} from "../../../src/interfaces/ITouchstoneHook.sol";
import {TouchstoneVault} from "../../../src/TouchstoneVault.sol";
import {Holdings} from "../../../src/types/Holdings.sol";

/// @notice The minimal P2 handler: deposits, withdrawals, time (snapshotted steps and silent jumps), keep-alive
///         refreshes, test-only placements, third-party swaps, reference moves and `breakRef` (every failure mode of
///         the scripted reference). Every withdrawal is checked against the exact pro-rata share computed from the
///         PoolManager's state (I-2) and against the revert allow-list (I-10); the results are kept in ghosts.
contract VaultHandler is Test {
    using StateLibrary for IPoolManager;

    IPoolManager internal immutable manager;
    VaultHarness internal immutable hv;
    ITouchstoneHook internal immutable hook;
    MockRefPool internal immutable refPool;
    MockToken internal immutable token0;
    MockToken internal immutable token1;
    PoolSwapTest internal immutable swapRouter;
    PoolKey internal key;
    PoolId internal immutable poolId;
    uint256 internal immutable blocktime;
    int24 internal immutable center;
    uint256 internal immutable swapMax0;
    uint256 internal immutable swapMax1;
    address[3] internal actors;

    // ghosts
    uint256 public withdrawals;
    uint256 public withdrawalsWithL;
    uint256 public placements;
    uint256 public breachesDriven;
    uint256 public exitedWithdrawals;
    uint256 public i2Failures;
    string public i2Why;
    uint256 public i10Failures;
    bytes4 public i10Selector;

    constructor(
        IPoolManager m,
        VaultHarness v,
        address h,
        MockRefPool r,
        MockToken t0,
        MockToken t1,
        PoolKey memory k,
        uint256 bt,
        int24 c,
        uint256 max0,
        uint256 max1
    ) {
        (manager, hv, hook, refPool, token0, token1) = (m, v, ITouchstoneHook(h), r, t0, t1);
        key = k;
        poolId = PoolId.wrap(keccak256(abi.encode(k)));
        (blocktime, center, swapMax0, swapMax1) = (bt, c, max0, max1);
        swapRouter = new PoolSwapTest(m);
        t0.mint(address(this), 1e40);
        t1.mint(address(this), 1e40);
        t0.approve(address(swapRouter), type(uint256).max);
        t1.approve(address(swapRouter), type(uint256).max);
        for (uint256 i; i < 3; ++i) {
            address a = address(uint160(0xA11CE0 + i));
            actors[i] = a;
            t0.mint(a, 1e40);
            t1.mint(a, 1e40);
            vm.startPrank(a);
            t0.approve(address(v), type(uint256).max);
            t1.approve(address(v), type(uint256).max);
            vm.stopPrank();
        }
    }

    // ------------------------------------------------------------------ flows

    function deposit(uint256 who, uint256 s) external {
        address a = actors[who % 3];
        uint256 S = hv.totalSupply();
        uint256 cap = uint256(hv.bondShares()) * 1e18 / hv.tier().BOND_RATIO;
        if (cap <= S) return;
        s = bound(s, 1, _min(cap - S, S / 4));
        vm.prank(a);
        try hv.deposit(s, type(uint256).max, type(uint256).max, a) {} catch {}
    }

    function withdraw(uint256 who, uint256 s) external {
        address a = actors[who % 3];
        uint256 bal = hv.balanceOf(a);
        if (bal == 0) return;
        s = bound(s, 1, bal);
        // the expectation, from the PoolManager's state in this block (the prologue swaps nothing; collect-first
        // sweeps the fees computed here; this build mints no bank shares, so the checkpoint burns none)
        Holdings memory h = hv.holdings();
        uint256 S = hv.totalSupply();
        (uint256 f0, uint256 f1) = ExpectedFlows.fees(manager, poolId, address(hv), h.lo, h.hi);
        uint128 dL = uint128(FullMath.mulDiv(h.L, s, S));
        (uint256 r0, uint256 r1) = ExpectedFlows.legs(manager, poolId, h.lo, h.hi, dL, false);
        uint256 e0 = FullMath.mulDiv(h.idle0 + f0, s, S) + r0;
        uint256 e1 = FullMath.mulDiv(h.idle1 + f1, s, S) + r1;
        uint256 b0 = token0.balanceOf(a);
        uint256 b1 = token1.balanceOf(a);
        vm.prank(a);
        try hv.withdraw(s, 0, 0, a) {
            ++withdrawals;
            if (h.L > 0) ++withdrawalsWithL;
            (,,,,, uint8 mode,) = hv.bank();
            if (mode == 2) ++exitedWithdrawals;
            if (token0.balanceOf(a) - b0 != e0) _i2("token0 payout");
            if (token1.balanceOf(a) - b1 != e1) _i2("token1 payout");
            if (h.L - hv.holdings().L != dL) _i2("liquidity removed");
        } catch (bytes memory err) {
            bytes4 sel = bytes4(err);
            bool allowed = sel == TouchstoneVault.ZeroShares.selector || sel == TouchstoneVault.Slippage.selector
                || sel == IERC20Errors.ERC20InsufficientBalance.selector;
            if (!allowed) {
                ++i10Failures;
                i10Selector = sel;
            }
        }
    }

    function place(uint256 w, uint256 off) external {
        (, int24 t,,) = manager.getSlot0(poolId);
        int24 width = int24(int256(bound(w, 2, 40))) * 20; // 40 … 800 ticks, a multiple of 2·spacing
        int24 lo = _floor10(t) - width / 2 + int24(int256(bound(off, 0, uint256(int256(width / 20) - 1)))) * 10;
        if (lo > t) lo -= 10;
        try hv.placeForTest(lo, lo + width) {
            ++placements;
        } catch {}
    }

    // ------------------------------------------------------------------ the market and time

    function swap(bool zeroForOne, uint256 amount, uint256 limit) external {
        amount = bound(amount, 1e3, zeroForOne ? swapMax0 : swapMax1);
        (, int24 t,,) = manager.getSlot0(poolId);
        int24 d = int24(int256(bound(limit, 1, 400)));
        int24 lim = zeroForOne ? t - d : t + d;
        if (lim < center - 5900) lim = center - 5900;
        if (lim > center + 5900) lim = center + 5900;
        if ((zeroForOne && lim >= t) || (!zeroForOne && lim <= t)) return;
        swapRouter.swap(
            key,
            SwapParams(zeroForOne, -int256(amount), TickMath.getSqrtPriceAtTick(lim)),
            PoolSwapTest.TestSettings({takeClaims: false, settleUsingBurn: false}),
            ""
        );
    }

    /// @notice 85%: 1-3 blocks, each snapshotted; 15%: a silent jump of 37-120 blocks.
    function advance(uint256 seed) external {
        if (seed % 100 < 85) {
            uint256 n = 1 + (seed >> 8) % 3;
            for (uint256 i; i < n; ++i) {
                _roll(1);
                hook.snapshotIfNewBlock();
            }
        } else {
            _roll(37 + (seed >> 8) % 84);
        }
    }

    /// @notice Holds the reference outside the vault's range, observed, until one more breach latches (bounded).
    function driveBreach() external {
        Holdings memory h = hv.holdings();
        refPool.setMode(MockRefPool.Mode.Ok);
        refPool.setTick(h.hi + 1000 > center + 1500 ? h.lo - 1000 : h.hi + 1000);
        uint32 seq = hook.obs().breachSeq;
        for (uint256 i; i < 40 && hook.obs().breachSeq == seq; ++i) {
            _roll(10);
            try hook.refreshRef() {} catch {}
        }
        if (hook.obs().breachSeq > seq) ++breachesDriven;
    }

    function refresh() external {
        try hook.refreshRef() {} catch {}
    }

    /// @notice Moves the reference by up to ±1,500 ticks from the pool's centre (breaches latch when it leaves the
    ///         vault's range; the reference starts in range).
    function moveRef(uint256 d) external {
        int24 t = center + int24(int256(bound(d, 0, 3000))) - 1500;
        refPool.setTick(t);
    }

    /// @notice Every failure mode of the scripted reference, and back (half the calls restore it).
    function breakRef(uint256 m) external {
        uint256 k = m % 10;
        if (k < 5) refPool.setMode(MockRefPool.Mode.Ok);
        else if (k == 5) refPool.setMode(MockRefPool.Mode.Old);
        else if (k == 6) refPool.setMode(MockRefPool.Mode.Revert);
        else if (k == 7) refPool.setMode(MockRefPool.Mode.Short);
        else if (k == 8) refPool.setMode(MockRefPool.Mode.GasBomb);
        else refPool.setMode(MockRefPool.Mode.ReturnBomb);
    }

    // ------------------------------------------------------------------ helpers

    function _i2(string memory why) internal {
        ++i2Failures;
        i2Why = why;
    }

    function _roll(uint256 n) internal {
        vm.roll(vm.getBlockNumber() + n);
        vm.warp(vm.getBlockTimestamp() + n * blocktime);
    }

    function _floor10(int24 t) internal pure returns (int24) {
        int24 f = (t / 10) * 10;
        return (t < 0 && f != t) ? f - 10 : f;
    }

    function _min(uint256 a, uint256 b) internal pure returns (uint256) {
        return a < b ? a : b;
    }
}
