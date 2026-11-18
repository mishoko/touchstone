// SPDX-License-Identifier: MIT
pragma solidity 0.8.26;

import {Script, console2} from "forge-std/Script.sol";
import {TickMath} from "@uniswap/v4-core/src/libraries/TickMath.sol";
import {Addresses} from "../Addresses.sol";
import {DemoToken, IV3Factory, IV3Pool, RefDriver} from "./DemoKit.sol";

/// @notice DEMO step 0b (spec §4.8), part 1: the mock tokens (mUSDC, 6 decimals, as token0 and mWETH, 18 decimals,
///         as token1, the orientation of the mainnet reference), the mock-token v3 reference pool through the
///         canonical v3 factory, its observation cardinality raised to MIN_CARD = 1,000 in chunks of 250, and
///         liquidity. Part 2 is the warm-up: reference swaps through the `RefDriver` for at least
///         1.5 x (GRID_SEC + REF_WINDOW) = 900 s before `DeployDemo.s.sol` runs.
///         Unichain Sepolia only (chain 1301): the script refuses any other chain.
///         Run: `forge script script/demo/DemoRef.s.sol --rpc-url <unichain sepolia> --private-key $DEPLOYER_PK --broadcast --slow`
contract DemoRef is Script {
    uint24 internal constant FEE = 500;
    /// @dev ETH at 3,000 USDC in raw units: 1e18 / 3e9 per unit of token0, tick ~196,256; on the 10-tick grid.
    int24 internal constant START_TICK = 196_250;
    int24 internal constant FULL_LO = -887_270;
    int24 internal constant FULL_HI = 887_270;
    /// @dev ~1,000 mWETH and ~3M mUSDC of full-range depth at the start price.
    uint128 internal constant DEPTH_L = 5.5e16;

    error WrongChain(uint256 chainId);

    function run() external {
        if (block.chainid != 1301) revert WrongChain(block.chainid);
        vm.startBroadcast();
        address me = msg.sender;
        // mUSDC must sort below mWETH: choose the creation order from the next two CREATE addresses
        uint64 n = vm.getNonce(me);
        bool usdcFirst = vm.computeCreateAddress(me, n) < vm.computeCreateAddress(me, n + 1);
        DemoToken usdc;
        DemoToken weth;
        if (usdcFirst) {
            usdc = new DemoToken("Touchstone Demo USDC", "mUSDC", 6);
            weth = new DemoToken("Touchstone Demo WETH", "mWETH", 18);
        } else {
            weth = new DemoToken("Touchstone Demo WETH", "mWETH", 18);
            usdc = new DemoToken("Touchstone Demo USDC", "mUSDC", 6);
        }
        require(address(usdc) < address(weth), "order");

        IV3Factory f = IV3Factory(Addresses.UNICHAIN_SEPOLIA_V3_FACTORY);
        IV3Pool pool = IV3Pool(f.createPool(address(usdc), address(weth), FEE));
        pool.initialize(TickMath.getSqrtPriceAtTick(START_TICK));
        for (uint16 c = 250; c <= 1000; c += 250) {
            pool.increaseObservationCardinalityNext(c);
        }
        RefDriver driver = new RefDriver(me, pool);
        usdc.mint(address(driver), 10_000_000e6);
        weth.mint(address(driver), 5_000e18);
        driver.addLiquidity(FULL_LO, FULL_HI, DEPTH_L);
        // the deployer's own balance funds the vault's seed and the curator later
        usdc.mint(me, 1_000_000e6);
        weth.mint(me, 1_000e18);
        vm.stopBroadcast();

        console2.log("mUSDC (token0)", address(usdc));
        console2.log("mWETH (token1)", address(weth));
        console2.log("v3 reference pool", address(pool));
        console2.log("RefDriver", address(driver));
    }
}
