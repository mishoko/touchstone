// SPDX-License-Identifier: MIT
pragma solidity 0.8.26;

import {IHooks} from "@uniswap/v4-core/src/interfaces/IHooks.sol";
import {Hooks} from "@uniswap/v4-core/src/libraries/Hooks.sol";
import {CustomRevert} from "@uniswap/v4-core/src/libraries/CustomRevert.sol";
import {TickMath} from "@uniswap/v4-core/src/libraries/TickMath.sol";
import {PoolKey} from "@uniswap/v4-core/src/types/PoolKey.sol";
import {SwapParams} from "@uniswap/v4-core/src/types/PoolOperation.sol";
import {StateLibrary} from "@uniswap/v4-core/src/libraries/StateLibrary.sol";
import {IPoolManager} from "@uniswap/v4-core/src/interfaces/IPoolManager.sol";
import {BaseHook} from "@openzeppelin/uniswap-hooks/base/BaseHook.sol";
import {ITouchstoneHook} from "../../src/interfaces/ITouchstoneHook.sol";
import {MedianOracle} from "../../src/libraries/MedianOracle.sol";
import {Obs} from "../../src/types/Obs.sol";
import {RealPoolBase} from "../utils/RealPoolBase.sol";
import {MockRefPool} from "../utils/MockRefPool.sol";

/// @notice The production hook on a live pool: swaps take the snapshot, the reference cannot block a swap, and
///         initialization, donation and foreign pool keys revert.
contract HookPoolTest is RealPoolBase {
    using StateLibrary for IPoolManager;

    function setUp() public {
        _setUpPool();
    }

    function test_U_1__firstSwapPerBlockRecordsTheBlockStartTick() public {
        advance(1);
        (, int24 t0,,) = IPoolManager(address(manager)).getSlot0(key.toId());
        (uint8 head0, uint8 count0) = hook.ringHeadCount();
        _swap(true, 1e18);
        (, int24 t1,,) = IPoolManager(address(manager)).getSlot0(key.toId());
        assertLt(t1, t0);
        _swap(true, 1e18); // a later swap in the same block records nothing
        (uint8 head1, uint8 count1) = hook.ringHeadCount();
        assertEq(count1, count0 + 1);
        assertEq(hook.ringAt(head0), MedianOracle.pack(vm.getBlockNumber(), t0));
        (, int24 t2,,) = IPoolManager(address(manager)).getSlot0(key.toId());
        advance(1);
        _swap(false, 1e18);
        assertEq(hook.ringAt(head1), MedianOracle.pack(vm.getBlockNumber(), t2));
    }

    function test_U_ref_fail__swapsSucceedInEveryReferenceMode() public {
        MockRefPool.Mode[5] memory modes = [
            MockRefPool.Mode.Old,
            MockRefPool.Mode.Revert,
            MockRefPool.Mode.Short,
            MockRefPool.Mode.GasBomb,
            MockRefPool.Mode.ReturnBomb
        ];
        for (uint256 i; i < modes.length; ++i) {
            ref.setMode(modes[i]);
            advance(tier.GRID); // a new anchor: the first swap of the block tries the reference
            uint40 cachedBefore = hook.obs().refCachedAt;
            _swap(i % 2 == 0, 1e17);
            (, uint8 count) = hook.ringHeadCount();
            assertGt(count, 0);
            assertEq(hook.obs().refCachedAt, cachedBefore);
            vm.expectRevert(ITouchstoneHook.RefUnreadable.selector);
            hook.liveRef();
        }
        _refConst(0);
        advance(1);
        _swap(true, 1e17);
        assertEq(hook.obs().refCachedAt, vm.getBlockNumber());
    }

    function test_D_1__reinitializeAndForeignKeysRevert() public {
        bytes memory reason = abi.encodePacked(BaseHook.HookNotImplemented.selector);
        bytes memory wrapped = abi.encodeWithSelector(
            CustomRevert.WrappedError.selector,
            address(hook),
            IHooks.beforeInitialize.selector,
            reason,
            abi.encodePacked(Hooks.HookCallFailed.selector)
        );
        // after: the pool key again
        vm.expectRevert(wrapped);
        manager.initialize(key, TickMath.getSqrtPriceAtTick(100));
        // a foreign key on this hook
        PoolKey memory foreign = PoolKey(key.currency0, key.currency1, 3000, 60, IHooks(address(hook)));
        vm.expectRevert(wrapped);
        manager.initialize(foreign, TickMath.getSqrtPriceAtTick(0));
    }

    function test_D_1__donateReverts() public {
        bytes memory reason = abi.encodePacked(BaseHook.HookNotImplemented.selector);
        vm.expectRevert(
            abi.encodeWithSelector(
                CustomRevert.WrappedError.selector,
                address(hook),
                IHooks.beforeDonate.selector,
                reason,
                abi.encodePacked(Hooks.HookCallFailed.selector)
            )
        );
        donateRouter.donate(key, 1e18, 1e18, "");
    }

    function test_D_1__poolIdCheckOnTheSwapCallback() public {
        PoolKey memory foreign = PoolKey(key.currency0, key.currency1, 3000, 60, IHooks(address(hook)));
        SwapParams memory p = SwapParams(true, -1, TickMath.MIN_SQRT_PRICE + 1);
        vm.prank(address(manager));
        vm.expectRevert(ITouchstoneHook.WrongPool.selector);
        hook.beforeSwap(address(this), foreign, p, "");
        // only the PoolManager may call the callbacks
        vm.expectRevert(BaseHook.NotPoolManager.selector);
        hook.beforeSwap(address(this), key, p, "");
    }

    /// The witness position counts tauB on the live pool tick.
    function test_U_clock__witnessCountsTauBOnThePoolTick() public {
        hook.setWitnessRange(-100, 100);
        advance(tier.GRID);
        hook.refreshRef();
        assertEq(hook.obs().tauB, tier.GRID);
        _swap(true, 1e20); // move the pool tick below the witness
        (, int24 t,,) = IPoolManager(address(manager)).getSlot0(key.toId());
        assertLt(t, -100);
        advance(tier.GRID);
        hook.refreshRef();
        assertEq(hook.obs().tauB, tier.GRID);
    }
}
