// SPDX-License-Identifier: MIT
pragma solidity 0.8.26;

import {Script, console2} from "forge-std/Script.sol";
import {TickMath} from "@uniswap/v4-core/src/libraries/TickMath.sol";
import {Currency} from "@uniswap/v4-core/src/types/Currency.sol";
import {LiquidityAmounts} from "@uniswap/v4-periphery/src/libraries/LiquidityAmounts.sol";
import {SqrtPriceMath} from "@uniswap/v4-core/src/libraries/SqrtPriceMath.sol";
import {IERC20} from "@openzeppelin/contracts/token/ERC20/IERC20.sol";
import {TouchstoneFactory} from "../../src/TouchstoneFactory.sol";
import {TouchstoneHook} from "../../src/TouchstoneHook.sol";
import {HookArgs, SeedParams} from "../../src/interfaces/ITouchstoneFactory.sol";
import {IUniswapV3PoolOracle} from "../../src/interfaces/IUniswapV3PoolOracle.sol";
import {TouchstoneVault} from "../../src/TouchstoneVault.sol";
import {TouchstoneVaultExtension} from "../../src/TouchstoneVaultExtension.sol";
import {CuratorBundler, DeployArgs} from "../../src/periphery/CuratorBundler.sol";
import {PreSwap} from "../../src/types/PreSwap.sol";
import {Tier} from "../../src/types/Tier.sol";
import {HookMining} from "../Mine.s.sol";
import {Tiers} from "../Tiers.sol";
import {Addresses} from "../Addresses.sol";

/// @notice DEMO deploy (spec §4.8 steps 1-4b) on Unichain Sepolia, after step 0b (`DemoRef.s.sol` and the warm-up):
///         the factory against the canonical v4 PoolManager, the curator's `CuratorBundler`, the hook salt mined for
///         the bundler, and `deployAndPlace` (factory.deploy with the seed, then the first placement, one
///         transaction). The DEMO tier takes the mock-token reference pool and the reference price read here.
///         Env: REF_POOL (the v3 reference pool from step 0b). The deployer holds the mock tokens (step 0b minted them).
///         Run: `REF_POOL=<pool> forge script script/demo/DeployDemo.s.sol --rpc-url <unichain sepolia> --private-key $DEPLOYER_PK --broadcast --slow`
contract DeployDemo is Script {
    uint24 internal constant FEE = 500;
    int24 internal constant SPACING = 10;
    /// @dev The seed's token1 side (mWETH): with its token0 counterpart, a seed worth ~4 mWETH, above BOND_MIN (1.74).
    uint256 internal constant SEED1 = 2e18;

    error WrongChain(uint256 chainId);
    error History();

    function run() external {
        if (block.chainid != 1301) revert WrongChain(block.chainid);
        address refPool = vm.envAddress("REF_POOL");
        (Tier memory t,) = Tiers.demo();
        t.REF_POOL = refPool;
        // the live reference (the tier's REF_WINDOW TWAP), and D-5's history check before factory.deploy
        int24 refTick = _twap(refPool, t.REF_WINDOW);
        uint32[] memory ago = new uint32[](2);
        ago[0] = t.GRID_SEC + t.REF_WINDOW;
        try IUniswapV3PoolOracle(refPool).observe(ago) {} catch {
            revert History();
        }
        uint160 seedSqrt = TickMath.getSqrtPriceAtTick(refTick);
        t.REF_SQRT_PRICE = seedSqrt;
        Tiers.check(t, 1);

        IUniswapV3PoolOracle r = IUniswapV3PoolOracle(refPool);
        IERC20 t0 = IERC20(r.token0());
        IERC20 t1 = IERC20(r.token1());

        vm.startBroadcast();
        address me = msg.sender;
        address pm = Addresses.UNICHAIN_SEPOLIA_POOL_MANAGER;
        bytes memory vaultCode = type(TouchstoneVault).creationCode;
        bytes memory extCode = type(TouchstoneVaultExtension).creationCode;
        bytes memory hookCode = type(TouchstoneHook).creationCode;
        TouchstoneFactory factory = new TouchstoneFactory(pm, keccak256(vaultCode), keccak256(extCode), keccak256(hookCode));
        CuratorBundler bundler = new CuratorBundler(me, factory);
        bytes32 saltV = bytes32(uint256(1));
        HookArgs memory args = HookArgs({
            poolManager: pm,
            vault: HookMining.vaultAddress(address(factory), address(bundler), saltV, vaultCode),
            currency0: Currency.wrap(address(t0)),
            currency1: Currency.wrap(address(t1)),
            fee: FEE,
            tickSpacing: SPACING,
            seedSqrtPriceX96: seedSqrt,
            tier: t
        });
        (bytes32 saltH,) = HookMining.mine(address(factory), address(bundler), hookCode, args, 0);

        // the first placement: W wide, aligned, containing spot (= the seed price) and the reference
        int24 c = refTick / SPACING * SPACING;
        if (refTick < 0 && refTick % SPACING != 0) c -= SPACING;
        int24 lo = c - t.W_TICKS / 2;
        int24 hi = c + t.W_TICKS / 2;
        uint160 sa = TickMath.getSqrtPriceAtTick(lo);
        uint160 sb = TickMath.getSqrtPriceAtTick(hi);
        uint128 l = LiquidityAmounts.getLiquidityForAmount1(sa, seedSqrt, SEED1);
        uint256 a0 = SqrtPriceMath.getAmount0Delta(seedSqrt, sb, l, true);
        uint256 a1 = SqrtPriceMath.getAmount1Delta(sa, seedSqrt, l, true);
        // 1% headroom for rounding up; the witness reserve is 1 mUSDC and 1e-6 mWETH
        SeedParams memory seed =
            SeedParams({a0: a0 * 101 / 100 + 1, a1: a1 * 101 / 100 + 1, curator: address(bundler), wit0: 1e6, wit1: 1e12});
        t0.transfer(address(bundler), seed.a0 + seed.wit0);
        t1.transfer(address(bundler), seed.a1 + seed.wit1);
        (address v, address h) = bundler.deployAndPlace(
            DeployArgs(vaultCode, extCode, hookCode, args, saltV, saltH, seed), lo, hi, PreSwap(false, false, 0), type(uint256).max
        );
        vm.stopBroadcast();

        console2.log("factory", address(factory));
        console2.log("bundler (curator)", address(bundler));
        console2.log("vault", v);
        console2.log("hook", h);
        console2.log("reference tick", int256(refTick));
        console2.log("range lo", int256(lo));
        console2.log("range hi", int256(hi));
    }

    function _twap(address pool, uint32 window) internal view returns (int24) {
        uint32[] memory ago = new uint32[](2);
        ago[0] = window;
        (int56[] memory c,) = IUniswapV3PoolOracle(pool).observe(ago);
        int56 d = c[1] - c[0];
        int24 tick = int24(d / int56(uint56(window)));
        if (d < 0 && d % int56(uint56(window)) != 0) --tick;
        return tick;
    }
}
