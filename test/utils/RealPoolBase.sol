// SPDX-License-Identifier: MIT
pragma solidity 0.8.26;

import {Test} from "forge-std/Test.sol";
import {PoolManager} from "@uniswap/v4-core/src/PoolManager.sol";
import {IPoolManager} from "@uniswap/v4-core/src/interfaces/IPoolManager.sol";
import {IHooks} from "@uniswap/v4-core/src/interfaces/IHooks.sol";
import {Hooks} from "@uniswap/v4-core/src/libraries/Hooks.sol";
import {TickMath} from "@uniswap/v4-core/src/libraries/TickMath.sol";
import {Currency} from "@uniswap/v4-core/src/types/Currency.sol";
import {PoolKey} from "@uniswap/v4-core/src/types/PoolKey.sol";
import {SwapParams, ModifyLiquidityParams} from "@uniswap/v4-core/src/types/PoolOperation.sol";
import {PoolSwapTest} from "@uniswap/v4-core/src/test/PoolSwapTest.sol";
import {PoolModifyLiquidityTest} from "@uniswap/v4-core/src/test/PoolModifyLiquidityTest.sol";
import {PoolDonateTest} from "@uniswap/v4-core/src/test/PoolDonateTest.sol";
import {HookArgs} from "../../src/interfaces/ITouchstoneFactory.sol";
import {Tier} from "../../src/types/Tier.sol";
import {Tiers} from "../../script/Tiers.sol";
import {HookExposed} from "./HookHarness.sol";
import {MockRefPool} from "./MockRefPool.sol";
import {MockToken} from "./Stubs.sol";
import {WitnessVault} from "./WitnessVault.sol";

/// @notice A live v4 pool with the production hook (plus test getters), third-party liquidity, swap / liquidity /
///         donate routers, a scripted reference and a vault stand-in that funds and rolls the witness. TEST tier.
abstract contract RealPoolBase is Test {
    uint160 internal constant FLAGS = Hooks.BEFORE_INITIALIZE_FLAG | Hooks.BEFORE_SWAP_FLAG | Hooks.BEFORE_DONATE_FLAG;

    PoolManager internal manager;
    MockToken internal token0;
    MockToken internal token1;
    MockRefPool internal ref;
    HookExposed internal hook;
    WitnessVault internal vault;
    PoolKey internal key;
    Tier internal tier;
    uint256 internal blocktime;
    PoolSwapTest internal swapRouter;
    PoolModifyLiquidityTest internal lpRouter;
    PoolDonateTest internal donateRouter;

    function _setUpPool() internal {
        (Tier memory t, uint256 bt) = Tiers.testing();
        blocktime = bt;
        vm.roll(10_000);
        vm.warp(10_000 * bt);
        manager = new PoolManager(address(this));
        MockToken a = new MockToken("A", 18);
        MockToken b = new MockToken("B", 18);
        (token0, token1) = address(a) < address(b) ? (a, b) : (b, a);
        ref = new MockRefPool(address(token0), address(token1), 500, 0, 0);
        _refConst(0);
        t.REF_POOL = address(ref);
        t.REF_SQRT_PRICE = TickMath.getSqrtPriceAtTick(0);
        tier = t;
        Currency c0 = Currency.wrap(address(token0));
        Currency c1 = Currency.wrap(address(token1));
        vault = new WitnessVault(manager, c0, c1);
        HookArgs memory args = HookArgs({
            poolManager: address(manager),
            vault: address(vault),
            currency0: c0,
            currency1: c1,
            fee: 500,
            tickSpacing: 10,
            seedSqrtPriceX96: TickMath.getSqrtPriceAtTick(0),
            tier: t
        });
        address at = address(uint160(0x4444 << 100) | FLAGS);
        deployCodeTo("HookHarness.sol:HookExposed", abi.encode(args), at);
        hook = HookExposed(at);
        vault.setHook(at);
        key = PoolKey(c0, c1, 500, 10, IHooks(at));

        swapRouter = new PoolSwapTest(manager);
        lpRouter = new PoolModifyLiquidityTest(manager);
        donateRouter = new PoolDonateTest(manager);
        token0.mint(address(this), 1e30);
        token1.mint(address(this), 1e30);
        token0.approve(address(swapRouter), type(uint256).max);
        token1.approve(address(swapRouter), type(uint256).max);
        token0.approve(address(lpRouter), type(uint256).max);
        token1.approve(address(lpRouter), type(uint256).max);
        token0.approve(address(donateRouter), type(uint256).max);
        token1.approve(address(donateRouter), type(uint256).max);
        lpRouter.modifyLiquidity(key, ModifyLiquidityParams(-6000, 6000, 1e21, 0), "");

        // seed: witness funding and arming, as the vault does
        token0.transfer(address(vault), 1e12);
        token1.transfer(address(vault), 1e12);
        vault.fund(1e12, 1e12);
        hook.snapshotIfNewBlock();
        vm.prank(address(vault));
        hook.armAtSeed();
    }

    function _refConst(int24 t) internal {
        ref.setMode(MockRefPool.Mode.Ok);
        uint32[] memory s = new uint32[](1);
        int24[] memory k = new int24[](1);
        k[0] = t;
        ref.setPath(s, k);
    }

    function _swap(bool zeroForOne, uint256 amountIn) internal {
        swapRouter.swap(
            key,
            SwapParams({
                zeroForOne: zeroForOne,
                amountSpecified: -int256(amountIn),
                sqrtPriceLimitX96: zeroForOne ? TickMath.MIN_SQRT_PRICE + 1 : TickMath.MAX_SQRT_PRICE - 1
            }),
            PoolSwapTest.TestSettings({takeClaims: false, settleUsingBurn: false}),
            ""
        );
    }

    function advance(uint256 n) internal {
        vm.roll(vm.getBlockNumber() + n);
        vm.warp(vm.getBlockTimestamp() + n * blocktime);
    }
}
