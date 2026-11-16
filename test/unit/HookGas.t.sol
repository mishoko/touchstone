// SPDX-License-Identifier: MIT
pragma solidity 0.8.26;

import {console} from "forge-std/Test.sol";
import {TickMath} from "@uniswap/v4-core/src/libraries/TickMath.sol";
import {IPoolManager} from "@uniswap/v4-core/src/interfaces/IPoolManager.sol";
import {StateLibrary} from "@uniswap/v4-core/src/libraries/StateLibrary.sol";
import {SwapParams} from "@uniswap/v4-core/src/types/PoolOperation.sol";
import {RealPoolBase} from "../utils/RealPoolBase.sol";

/// @notice Gas of the hook's swap callback on a live pool, with the hook's storage and account cold (each swap is
///         its own transaction on chain). Steady state: the ring is full and a range is registered.
contract HookGasTest is RealPoolBase {
    using StateLibrary for IPoolManager;

    uint256 internal constant G1_MAX = 30_000; // first swap per block
    uint256 internal constant G2_MAX = 6_000; // later swaps in the block

    function setUp() public {
        _setUpPool();
        hook.snapshotIfNewBlock();
        vm.prank(address(vault));
        hook.setVaultRange(-1000, 1000, 0);
        hook.setWitnessRange(-1000, 1000);
        for (uint256 i; i < 40; ++i) {
            advance(1);
            _swap(i % 2 == 0, 1e16);
        }
        // move to an anchor's first block and refresh, so that the next blocks of the anchor do not read
        uint256 b = vm.getBlockNumber();
        advance((b / tier.GRID + 1) * tier.GRID - b);
        hook.refreshRef();
    }

    function _callbackGas() internal returns (uint256 used) {
        SwapParams memory p = SwapParams(true, -1e16, TickMath.MIN_SQRT_PRICE + 1);
        // In a swap the PoolManager is the caller and has just read the pool's slot0: both are warm. The hook's
        // account and storage are cold.
        IPoolManager(address(manager)).getSlot0(key.toId());
        vm.cool(address(hook));
        vm.prank(address(manager));
        uint256 g = gasleft();
        hook.beforeSwap(address(swapRouter), key, p, "");
        used = g - gasleft();
    }

    function _record(string memory name, uint256 v) internal {
        console.log(name, v);
        if (vm.envOr("TS_MUTATION_RUN", uint256(0)) == 0) vm.snapshotValue("TouchstoneHook", name, v);
    }

    function test_G_1__firstSwapPerBlockExtraGas() public {
        advance(3); // inside the anchor: no reference read, no grid boundary
        uint256 used = _callbackGas();
        _record("G-1 first swap per block", used);
        assertLe(used, G1_MAX);
    }

    function test_G_1b__refreshBlockExtraGas() public {
        advance(tier.GRID); // the next anchor's first block: reads the reference and freezes one boundary
        uint256 used = _callbackGas();
        _record("G-1b refresh block (MockRefPool)", used);
        assertEq(hook.obs().refCachedAt, vm.getBlockNumber());
        assertLe(used, uint256(tier.REFRESH_GAS_MIN) + 20_000);
    }

    function test_G_2__laterSwapInTheBlockExtraGas() public {
        advance(3);
        _callbackGas();
        uint256 used = _callbackGas();
        _record("G-2 later swap in the block", used);
        assertLe(used, G2_MAX);
    }
}
