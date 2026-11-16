// SPDX-License-Identifier: MIT
pragma solidity 0.8.26;

import {IHooks} from "@uniswap/v4-core/src/interfaces/IHooks.sol";
import {PoolKey} from "@uniswap/v4-core/src/types/PoolKey.sol";
import {PoolId} from "@uniswap/v4-core/src/types/PoolId.sol";
import {Currency} from "@uniswap/v4-core/src/types/Currency.sol";
import {StateLibrary} from "@uniswap/v4-core/src/libraries/StateLibrary.sol";
import {TickMath} from "@uniswap/v4-core/src/libraries/TickMath.sol";
import {SwapParams, ModifyLiquidityParams} from "@uniswap/v4-core/src/types/PoolOperation.sol";
import {PoolSwapTest} from "@uniswap/v4-core/src/test/PoolSwapTest.sol";
import {PoolModifyLiquidityTest} from "@uniswap/v4-core/src/test/PoolModifyLiquidityTest.sol";
import {SeedParams} from "../../src/interfaces/ITouchstoneFactory.sol";
import {HarnessVaultBase} from "./HarnessVaultBase.sol";

/// @notice The factory-deployed harness on a pool with third-party liquidity and a swap router. Two price settings:
///         tick 0 (18/18 decimals) and tick 196,000 with token0 at 6 decimals and token1 at 18 (a USDC/WETH-like pool).
abstract contract LiquidPoolBase is HarnessVaultBase {
    using StateLibrary for *;

    PoolKey internal key;
    PoolId internal poolId;
    PoolSwapTest internal swapRouter;
    PoolModifyLiquidityTest internal lpRouter;

    /// @dev true: the USDC/WETH-like setting (tick 196,000, token0 at 6 decimals, token1 at 18); false: tick 0, 18/18.
    function _highTick() internal pure virtual returns (bool) {
        return false;
    }

    function _seedTick() internal view override returns (int24) {
        return _highTick() ? int24(196_000) : int24(0);
    }

    function _token0Decimals() internal view override returns (uint8) {
        return _highTick() ? 6 : 0;
    }

    function _seedParams() internal view override returns (SeedParams memory) {
        if (_highTick()) return SeedParams({a0: 3000e6, a1: 1e18, curator: curator, wit0: 1e6, wit1: 1e12});
        return super._seedParams();
    }

    /// @dev Third-party liquidity on [seed - 6000, seed + 6000).
    function _thirdPartyL() internal view returns (uint256) {
        return _highTick() ? 1e16 : 1e21;
    }

    function setUp() public virtual override {
        super.setUp();
        key = PoolKey(Currency.wrap(address(token0)), Currency.wrap(address(token1)), FEE, TICK_SPACING, IHooks(hook));
        poolId = key.toId();
        swapRouter = new PoolSwapTest(manager);
        lpRouter = new PoolModifyLiquidityTest(manager);
        token0.mint(address(this), 1e36);
        token1.mint(address(this), 1e36);
        token0.approve(address(swapRouter), type(uint256).max);
        token1.approve(address(swapRouter), type(uint256).max);
        token0.approve(address(lpRouter), type(uint256).max);
        token1.approve(address(lpRouter), type(uint256).max);
        int24 c = _seedTick();
        lpRouter.modifyLiquidity(key, ModifyLiquidityParams(c - 6000, c + 6000, int256(_thirdPartyL()), 0), "");
        _fundUser(alice, 1e36);
        _fundUser(bob, 1e36);
        _deposit(alice, 1e23);
    }

    function _tick() internal view returns (int24 t) {
        (, t,,) = manager.getSlot0(poolId);
    }

    /// @notice An exact-input swap by the test contract, stopped `limitTicks` away from the current tick.
    function _swap(bool zeroForOne, uint256 amountIn, int24 limitTicks) internal {
        int24 t = _tick();
        int24 lim = zeroForOne ? t - limitTicks : t + limitTicks;
        if (lim < TickMath.MIN_TICK + 1) lim = TickMath.MIN_TICK + 1;
        if (lim > TickMath.MAX_TICK - 1) lim = TickMath.MAX_TICK - 1;
        swapRouter.swap(
            key,
            SwapParams({
                zeroForOne: zeroForOne,
                amountSpecified: -int256(amountIn),
                sqrtPriceLimitX96: TickMath.getSqrtPriceAtTick(lim)
            }),
            PoolSwapTest.TestSettings({takeClaims: false, settleUsingBurn: false}),
            ""
        );
    }
}

