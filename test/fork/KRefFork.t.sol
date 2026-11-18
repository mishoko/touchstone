// SPDX-License-Identifier: MIT
pragma solidity 0.8.26;

import {console} from "forge-std/Test.sol";
import {IERC20} from "@openzeppelin/contracts/token/ERC20/IERC20.sol";
import {IPoolManager} from "@uniswap/v4-core/src/interfaces/IPoolManager.sol";
import {StateLibrary} from "@uniswap/v4-core/src/libraries/StateLibrary.sol";
import {TickMath} from "@uniswap/v4-core/src/libraries/TickMath.sol";
import {FullMath} from "@uniswap/v4-core/src/libraries/FullMath.sol";
import {TouchstoneHook} from "../../src/TouchstoneHook.sol";
import {TouchstoneVault} from "../../src/TouchstoneVault.sol";
import {VaultCommon} from "../../src/TouchstoneVaultBase.sol";
import {PreSwap} from "../../src/types/PreSwap.sol";
import {Addresses} from "../../script/Addresses.sol";
import {VaultView} from "../utils/VaultView.sol";
import {MainnetVaultFork} from "./MainnetVaultFork.sol";

using VaultView for TouchstoneVault;

interface IV3Pool {
    function swap(address to, bool zeroForOne, int256 amount, uint160 sqrtPriceLimitX96, bytes calldata data)
        external
        returns (int256 amount0, int256 amount1);
    function slot0()
        external
        view
        returns (uint160 sqrtPriceX96, int24 tick, uint16, uint16, uint16, uint8, bool);
    function liquidity() external view returns (uint128);
}

/// @notice Pushes the deep reference pool directly (pays in its swap callback from its own balance).
contract V3Pusher {
    IV3Pool internal immutable pool;
    IERC20 internal immutable t0;
    IERC20 internal immutable t1;

    constructor(IV3Pool p, IERC20 a, IERC20 b) {
        (pool, t0, t1) = (p, a, b);
    }

    /// @dev Swaps until the price reaches `limit` (or the input is exhausted); returns the amounts paid (> 0) and
    ///      received (< 0) as the pool reports them.
    function push(bool zeroForOne, uint160 limit) external returns (int256 a0, int256 a1) {
        return pool.swap(address(this), zeroForOne, type(int128).max, limit, "");
    }

    function uniswapV3SwapCallback(int256 a0, int256 a1, bytes calldata) external {
        require(msg.sender == address(pool), "pool");
        if (a0 > 0) t0.transfer(msg.sender, uint256(a0));
        if (a1 > 0) t1.transfer(msg.sender, uint256(a1));
    }
}

/// @notice K-ref (spec §9.1) on an Ethereum mainnet fork against the reference pool 0x88e6…5640 (chain 1, through
///         `ForkBase._fork`; `FORK_BLOCK_1=<n>` pins the block). With G-1b (`HookGasFork.t.sol`: the gas of both reads
///         at the live cardinality and on a full 65,535 ring, `REF_GAS_CAP`; the markout read `tryWindow`), it covers:
///         - the hook's reference read on real data equals an independent TWAP of `observe` (floor rounding);
///         - the fence on real data: a placement with spot beyond `REF_BAND` of the live reference reverts `Fence`, the
///           same placement with spot at the reference passes and records a charge and a reserve;
///         - the deep pool's push cost per tick on its real liquidity (reported, with `L_deep` = `liquidity()`): the
///           in-transaction round trip (both fee legs) and the one-block hold (the push leg's loss at the pre-push
///           price, which an arbitrageur takes back each block), for t in {10, 30, 60, 120} ticks; holding the
///           10-minute TWAP t ticks off costs about 50 one-block holds (12 s blocks).
contract KRefForkTest is MainnetVaultFork {
    using StateLibrary for IPoolManager;

    IV3Pool internal deep = IV3Pool(Addresses.MAINNET_REF_POOL_USDC_WETH_500);

    function test_K_ref__hookReadEqualsAnIndependentTwapOnRealData() public {
        if (!_setUp()) return;
        TouchstoneHook h = TouchstoneHook(hook);
        (bool ok, int24 t) = h.tryWindow(tier.REF_WINDOW, 0);
        assertTrue(ok, "the capped read succeeds on the real pool");
        assertEq(t, _twapTick(tier.REF_WINDOW), "hook read = independent floor TWAP");
        (bool ok2, int24 t2) = h.tryWindow(tier.REF_WINDOW + 3600, 3600);
        assertTrue(ok2, "an anchored window an hour back reads");
        console.log("K-ref TWAP now / one hour back");
        console.logInt(t);
        console.logInt(t2);
    }

    function _placeAround(int24 refTick, uint160 align) internal {
        int24 c = refTick / 10 * 10;
        if (refTick < 0 && refTick % 10 != 0) c -= 10;
        vm.prank(curator);
        bundler.moveAndPlace(
            align, 1, c - tier.W_TICKS / 2, c + tier.W_TICKS / 2, PreSwap(false, false, 0), 0, type(uint256).max
        );
    }

    function test_K_ref__fenceOnRealData() public {
        if (!_setUp()) return;
        deal(address(usdc), address(bundler), 1e13);
        deal(address(weth), address(bundler), 1e22);
        vm.roll(vm.getBlockNumber() + tier.COOLDOWN);
        vm.warp(vm.getBlockTimestamp() + tier.COOLDOWN * 12);
        int24 r = _twapTick(tier.REF_WINDOW);
        int24 out = r + int24(uint24(tier.REF_BAND)) + 20;
        uint160 sOut = TickMath.getSqrtPriceAtTick(out);
        vm.prank(curator);
        bundler.moveSpot(sOut);
        vm.expectRevert(VaultCommon.Fence.selector);
        _placeAround(r, sOut);
        uint160 sRef = TickMath.getSqrtPriceAtTick(r);
        vm.prank(curator);
        bundler.moveSpot(sRef);
        _placeAround(r, sRef);
        (uint40 tA,,, uint128 c0, uint128 reserve) = TouchstoneVault(payable(vault)).pending();
        assertEq(tA, vm.getBlockTimestamp(), "placed against the live reference");
        assertGt(reserve, 0, "a reserve held until the markout settles");
        console.log("K-ref fence: reference tick, band, charge0, reserve");
        console.logInt(r);
        console.log(uint256(uint24(tier.REF_BAND)), uint256(c0), uint256(reserve));
    }

    /// @dev Value in WETH (token1) of (a0, a1) at sqrt price `s` (token1 per token0 = s^2 / 2^192).
    function _v1(int256 a0, int256 a1, uint160 s) internal pure returns (int256) {
        uint256 p = FullMath.mulDiv(uint256(s), uint256(s), 1 << 64); // token1 per token0, X128
        int256 v0 = a0 >= 0
            ? int256(FullMath.mulDiv(uint256(a0), p, 1 << 128))
            : -int256(FullMath.mulDiv(uint256(-a0), p, 1 << 128));
        return v0 + a1;
    }

    function test_K_ref__deepPoolPushCostPerTick() public {
        if (!_fork("MAINNET_RPC_URL", 1)) return;
        IERC20 t0 = _token(0);
        IERC20 t1 = _token(1);
        V3Pusher p = new V3Pusher(deep, t0, t1);
        deal(address(t0), address(p), 1e15);
        deal(address(t1), address(p), 1e24);
        (uint160 s0, int24 tick0,,,,,) = deep.slot0();
        uint128 lDeep = deep.liquidity();
        console.log("K-ref L_deep (liquidity at the fork block)", lDeep);
        console.logInt(tick0);
        assertGt(lDeep, 0, "live liquidity");
        int24[4] memory ts = [int24(10), 30, 60, 120];
        uint256 snap = vm.snapshotState();
        int256 prevHold;
        for (uint256 i; i < 4; ++i) {
            vm.revertToState(snap);
            // push up by t ticks (token1 in), then return to the pre-push price (token0 in): the round trip
            (int256 u0, int256 u1) = p.push(false, TickMath.getSqrtPriceAtTick(tick0 + ts[i]));
            int256 hold = _v1(u0, u1, s0); // the push leg's loss at the pre-push price
            (int256 d0, int256 d1) = p.push(true, s0);
            int256 round = _v1(u0 + d0, u1 + d1, s0);
            (uint160 sBack,,,,,,) = deep.slot0();
            assertEq(sBack, s0, "returned to the pre-push price");
            console.log("K-ref push t ticks; one-block hold (wei WETH); round trip (wei WETH); notional WETH in");
            console.logInt(ts[i]);
            console.logInt(hold);
            console.logInt(round);
            console.logInt(u1);
            assertGt(hold, 0, "a push loses money at the pre-push price");
            assertGt(round, 0, "a round trip pays both fee legs");
            assertGt(hold, prevHold, "the hold cost grows with t");
            prevHold = hold;
        }
    }

    function _token(uint256 i) internal view returns (IERC20) {
        (bool ok, bytes memory r) = address(deep).staticcall(abi.encodeWithSignature(i == 0 ? "token0()" : "token1()"));
        require(ok);
        return IERC20(abi.decode(r, (address)));
    }
}
