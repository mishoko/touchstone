// SPDX-License-Identifier: MIT
pragma solidity 0.8.26;

import {Test, console} from "forge-std/Test.sol";
import {Hooks} from "@uniswap/v4-core/src/libraries/Hooks.sol";
import {TickMath} from "@uniswap/v4-core/src/libraries/TickMath.sol";
import {Currency} from "@uniswap/v4-core/src/types/Currency.sol";
import {HookArgs} from "../../src/interfaces/ITouchstoneFactory.sol";
import {IUniswapV3PoolOracle} from "../../src/interfaces/IUniswapV3PoolOracle.sol";
import {TouchstoneHook} from "../../src/TouchstoneHook.sol";
import {Tier} from "../../src/types/Tier.sol";
import {Tiers} from "../../script/Tiers.sol";
import {Addresses} from "../../script/Addresses.sol";

/// @notice G-1b on a mainnet fork: the gas of the reference read against USDC/WETH 0.05% and of a snapshot that
///         refreshes, at the pool's observed cardinality and with the cardinality forced to 65,535.
///         Runs with `FOUNDRY_PROFILE=fork forge test --mp test/fork`; skips when MAINNET_RPC_URL is unset.
contract HookGasForkTest is Test {
    uint160 internal constant FLAGS = Hooks.BEFORE_INITIALIZE_FLAG | Hooks.BEFORE_SWAP_FLAG | Hooks.BEFORE_DONATE_FLAG;
    address internal constant VAULT = address(0x5A17);

    IUniswapV3PoolOracle internal refPool = IUniswapV3PoolOracle(Addresses.MAINNET_REF_POOL_USDC_WETH_500);
    TouchstoneHook internal hook;
    Tier internal tier;

    function _setUpFork() internal returns (bool) {
        string memory url = vm.envOr("MAINNET_RPC_URL", string(""));
        if (bytes(url).length == 0) {
            vm.skip(true);
            return false;
        }
        vm.createSelectFork(url);
        (Tier memory t,) = Tiers.mainnet();
        tier = t;
        (uint160 sqrtRef,,,,,,) = refPool.slot0();
        HookArgs memory a = HookArgs({
            poolManager: Addresses.MAINNET_POOL_MANAGER,
            vault: VAULT,
            currency0: Currency.wrap(refPool.token0()),
            currency1: Currency.wrap(refPool.token1()),
            fee: 500,
            tickSpacing: 10,
            seedSqrtPriceX96: sqrtRef,
            tier: t
        });
        address at = address(uint160(0x7777 << 100) | FLAGS);
        deployCodeTo("TouchstoneHook.sol:TouchstoneHook", abi.encode(a), at);
        hook = TouchstoneHook(at);
        hook.snapshotIfNewBlock();
        vm.prank(VAULT);
        hook.armAtSeed();
        return true;
    }

    function _forceCardinality(uint16 c) internal {
        bytes32 s0 = vm.load(address(refPool), 0);
        uint256 v = uint256(s0);
        v &= ~(uint256(0xffff) << 200); // observationCardinality: bits 200..215
        v |= uint256(c) << 200;
        vm.store(address(refPool), 0, bytes32(v));
    }

    function _measure(string memory label) internal {
        vm.cool(address(refPool));
        uint256 g = gasleft();
        (bool ok,) = hook.tryRef();
        uint256 read = g - gasleft();
        assertTrue(ok, "reference readable");
        // the next anchor's first block: the snapshot reads the anchored TWAP
        vm.roll(vm.getBlockNumber() + tier.GRID);
        vm.warp(vm.getBlockTimestamp() + tier.GRID_SEC);
        vm.cool(address(refPool));
        vm.cool(address(hook));
        g = gasleft();
        hook.refreshRef();
        uint256 refreshBlock = g - gasleft();
        console.log(label, read, refreshBlock);
        assertEq(hook.obs().refCachedAt, vm.getBlockNumber());
        assertLe(read * 3 / 2, tier.REF_GAS_CAP, "REF_GAS_CAP is at least 1.5x the read");
        assertLe(refreshBlock, uint256(tier.REFRESH_GAS_MIN) + 20_000);
    }

    function test_G_1b__refreshBlockGasOnMainnetFork() public {
        if (!_setUpFork()) return;
        _measure("observed cardinality: read, refresh block");
        _forceCardinality(65_535);
        _measure("cardinality 65535: read, refresh block");
    }
}
