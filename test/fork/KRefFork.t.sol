// SPDX-License-Identifier: MIT
pragma solidity 0.8.26;

import {console} from "forge-std/Test.sol";
import {IERC20} from "@openzeppelin/contracts/token/ERC20/IERC20.sol";
import {IPoolManager} from "@uniswap/v4-core/src/interfaces/IPoolManager.sol";
import {StateLibrary} from "@uniswap/v4-core/src/libraries/StateLibrary.sol";
import {PoolId} from "@uniswap/v4-core/src/types/PoolId.sol";
import {TickMath} from "@uniswap/v4-core/src/libraries/TickMath.sol";
import {FullMath} from "@uniswap/v4-core/src/libraries/FullMath.sol";
import {TouchstoneHook} from "../../src/TouchstoneHook.sol";
import {TouchstoneVault} from "../../src/TouchstoneVault.sol";
import {VaultCommon} from "../../src/TouchstoneVaultBase.sol";
import {PreSwap} from "../../src/types/PreSwap.sol";
import {Holdings} from "../../src/types/Holdings.sol";
import {ExecLedger} from "../../src/libraries/ExecLedger.sol";
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
    function observe(uint32[] calldata ago) external view returns (int56[] memory, uint160[] memory);
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
        assertEq(t2, _twapAt(tier.REF_WINDOW + 3600, 3600), "anchored read = independent floor TWAP");
        console.log("K-ref TWAP now / one hour back");
        console.logInt(t);
        console.logInt(t2);
    }

    function _twapAt(uint32 a, uint32 b) internal view returns (int24 tick) {
        uint32[] memory ago = new uint32[](2);
        (ago[0], ago[1]) = (a, b);
        (int56[] memory c,) = deep.observe(ago);
        int56 d = c[1] - c[0];
        int56 w = int56(uint56(a - b));
        tick = int24(d / w);
        if (d < 0 && d % w != 0) --tick;
    }

    function _placeAround(int24 refTick, uint160 align) internal {
        int24 c = refTick / 10 * 10;
        if (refTick < 0 && refTick % 10 != 0) c -= 10;
        vm.prank(curator);
        bundler.moveAndPlace(
            align, 1, c - tier.W_TICKS / 2, c + tier.W_TICKS / 2, PreSwap(false, false, 0), 0, type(uint256).max
        );
    }

    function _prepAction() internal returns (int24 r) {
        deal(address(usdc), address(bundler), 1e13);
        deal(address(weth), address(bundler), 1e22);
        vm.roll(vm.getBlockNumber() + tier.COOLDOWN);
        vm.warp(vm.getBlockTimestamp() + tier.COOLDOWN * 12);
        r = _twapTick(tier.REF_WINDOW);
    }

    function _fenceRevertsAt(int24 r, int24 spot) internal {
        uint160 s = TickMath.getSqrtPriceAtTick(spot);
        vm.prank(curator);
        bundler.moveSpot(s);
        vm.expectRevert(VaultCommon.Fence.selector);
        _placeAround(r, s);
    }

    /// @notice The fence on real data, both sides: spot `REF_BAND + 20` ticks above or below the live reference
    ///         reverts `Fence`; the same placement with spot at the reference passes and holds a reserve.
    function test_K_ref__fenceOnRealData() public {
        if (!_setUp()) return;
        int24 r = _prepAction();
        int24 band = int24(uint24(tier.REF_BAND));
        _fenceRevertsAt(r, r + band + 20);
        _fenceRevertsAt(r, r - band - 20);
        uint160 sRef = TickMath.getSqrtPriceAtTick(r);
        vm.prank(curator);
        bundler.moveSpot(sRef);
        _placeAround(r, sRef);
        (uint40 tA,,,, uint128 reserve) = TouchstoneVault(payable(vault)).pending();
        assertEq(tA, vm.getBlockTimestamp(), "placed against the live reference");
        assertGt(reserve, 0, "a reserve held until the markout settles");
    }

    /// @dev The NAV loss of the recorded action (holdings before − after) valued at `p`, signed.
    function _leg(uint160 p) internal view returns (int256) {
        (, Holdings memory b, Holdings memory a,,) = TouchstoneVault(payable(vault)).pending();
        (uint256 nb,) = ExecLedger.value(b, p);
        (uint256 na,) = ExecLedger.value(a, p);
        return int256(nb) - int256(na);
    }

    struct Legs {
        uint256 charge;
        int256 post;
        int256 pre;
        int256 ref;
        uint128 amountIn;
    }

    /// @dev Spot `off` ticks from the live reference (inside the band), then the curator's `rebalanceSwap` buying
    ///      token0 with `amountIn` of WETH; returns the recorded charge and the three legs of spec §3.2 (the post-move
    ///      spot, the pre-move spot and the live reference) recomputed from the recorded holdings.
    function _swapAt(int24 off, uint128 amountIn) internal returns (Legs memory l) {
        int24 r = _prepAction();
        // re-place on a range shifted up by W/4 (still containing spot and the reference): it needs less WETH, so the
        // excess becomes idle, which pays the swap's input
        int24 cc = r / 10 * 10;
        if (r < 0 && r % 10 != 0) cc -= 10;
        int24 sh = tier.W_TICKS / 4 / 10 * 10;
        vm.prank(curator);
        bundler.moveSpot(TickMath.getSqrtPriceAtTick(r));
        vm.prank(curator);
        bundler.moveAndPlace(
            TickMath.getSqrtPriceAtTick(r), 1, cc - tier.W_TICKS / 2 + sh, cc + tier.W_TICKS / 2 + sh,
            PreSwap(false, false, 0), 0, type(uint256).max
        );
        r = _prepAction(); // the live reference after the cooldown (the TWAP window slides onto the fork-block tick)
        (bool ok, int24 hr) = TouchstoneHook(hook).tryWindow(tier.REF_WINDOW, 0);
        assertTrue(ok);
        assertEq(hr, r, "the hook reads the same reference");
        uint160 s0 = TickMath.getSqrtPriceAtTick(r + off);
        vm.prank(curator);
        bundler.moveSpot(s0);
        if (amountIn == 0) {
            amountIn = TouchstoneVault(payable(vault)).holdings().idle1 / 4; // idle pays the input
            if (amountIn > 0.02e18) amountIn = 0.02e18; // about 0.5% of NAV, inside MAX_SWAP_ACTION (2%)
        }
        assertGt(amountIn, 0, "idle WETH to swap");
        l.amountIn = amountIn;
        vm.prank(curator);
        bundler.rebalanceSwap(false, amountIn, type(uint256).max);
        (uint160 s1,,,) = pm.getSlot0(key.toId());
        uint128 c;
        (,,, c,) = TouchstoneVault(payable(vault)).pending();
        (l.charge, l.post, l.pre, l.ref) = (c, _leg(s1), _leg(s0), _leg(TickMath.getSqrtPriceAtTick(r)));
        int256 w = l.post;
        if (l.pre > w) w = l.pre;
        if (l.ref > w) w = l.ref;
        assertEq(l.charge, w > 0 ? uint256(w) : 0, "charge = worst leg (post spot, pre spot, reference), floored at 0");
    }

    /// @notice The reference charge leg on real data. The curator's `rebalanceSwap` buys USDC with 0.02 WETH of the vault's idle
    ///         WETH (≈ 0.5% of NAV) (idle pays a swap's input; the idle comes from a re-placement on a range shifted up by W/4), once with spot at the live reference and once with spot `REF_BAND / 2` above it
    ///         (USDC dearer at spot than at the reference, inside the fence). Each recorded charge equals the spec's
    ///         worst leg recomputed from the recorded holdings. Above the reference the reference leg binds (it exceeds
    ///         both spot legs) and the charge exceeds the at-reference charge: the gap between spot and the reference is
    ///         charged (the positive control is the at-reference swap, where the legs coincide).
    function test_K_ref__referenceChargeLegOnRealData() public {
        if (!_setUp()) return;
        uint256 snap = vm.snapshotState();
        Legs memory at = _swapAt(0, 0);
        vm.revertToState(snap);
        Legs memory up = _swapAt(int24(uint24(tier.REF_BAND)) / 2, at.amountIn);
        console.log("K-ref rebalanceSwap WETH in; charge at the reference, at +band/2", at.amountIn, at.charge, up.charge);
        console.log("K-ref legs at +band/2 (post, pre, reference)");
        console.logInt(up.post);
        console.logInt(up.pre);
        console.logInt(up.ref);
        assertGt(up.ref, up.post, "the reference leg exceeds the post-move spot leg");
        assertGt(up.ref, up.pre, "the reference leg exceeds the pre-move spot leg");
        assertEq(up.charge, uint256(up.ref), "the reference leg is the charge");
        assertGt(up.charge, at.charge, "spot off the reference is charged the gap");
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
