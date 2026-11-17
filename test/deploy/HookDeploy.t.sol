// SPDX-License-Identifier: MIT
pragma solidity 0.8.26;

import {IHooks} from "@uniswap/v4-core/src/interfaces/IHooks.sol";
import {IPoolManager} from "@uniswap/v4-core/src/interfaces/IPoolManager.sol";
import {Hooks} from "@uniswap/v4-core/src/libraries/Hooks.sol";
import {StateLibrary} from "@uniswap/v4-core/src/libraries/StateLibrary.sol";
import {TickMath} from "@uniswap/v4-core/src/libraries/TickMath.sol";
import {PoolKey} from "@uniswap/v4-core/src/types/PoolKey.sol";
import {PoolId} from "@uniswap/v4-core/src/types/PoolId.sol";
import {HookArgs} from "../../src/interfaces/ITouchstoneFactory.sol";
import {ITouchstoneHook} from "../../src/interfaces/ITouchstoneHook.sol";
import {TouchstoneHook} from "../../src/TouchstoneHook.sol";
import {HookMining} from "../../script/Mine.s.sol";
import {DeployBaseUnplaced} from "../utils/DeployBaseUnplaced.sol";

/// @notice The hook deployed through the factory (stand-in vault): initialization front-running, range-registration
///         access, and the constructor's reference checks.
contract HookDeployTest is DeployBaseUnplaced {
    using StateLibrary for IPoolManager;

    address internal attacker = makeAddr("attacker");

    function setUp() public override {
        _setUpEnv();
    }

    function _predict() internal returns (address v, address h, bytes32 sH) {
        v = HookMining.vaultAddress(address(factory), deployer, SALT_V, _vaultCode());
        hookArgs = _hookArgsFor(v);
        (sH, h) = HookMining.mine(address(factory), deployer, _hookCode(), hookArgs, 0);
    }

    function _keyFor(address h) internal view returns (PoolKey memory) {
        return PoolKey(hookArgs.currency0, hookArgs.currency1, FEE, TICK_SPACING, IHooks(h));
    }

    // ================================================================== D-1

    /// Before: initializing the pool at the predicted hook address fails (no hook code yet); the deploy then
    /// initializes it at the seed price.
    function test_D_1__frontRunBeforeDeployFailsAndSeedPriceHolds() public {
        (, address h,) = _predict();
        vm.prank(attacker);
        vm.expectRevert(); // the PoolManager rejects an empty hook response
        IPoolManager(address(manager)).initialize(_keyFor(h), TickMath.getSqrtPriceAtTick(5000));
        (, hook) = _deploy(deployer, SALT_V);
        assertEq(hook, h);
        (uint160 sqrtP,,,) = IPoolManager(address(manager)).getSlot0(_keyFor(h).toId());
        assertEq(sqrtP, hookArgs.seedSqrtPriceX96);
        assertEq(PoolId.unwrap(_keyFor(h).toId()), PoolId.unwrap(TouchstoneHook(h).POOL_ID()));
    }

    /// After: the same key and any foreign key on this hook revert in beforeInitialize.
    function test_D_1__initializeAfterDeployReverts() public {
        (, hook) = _deploy(deployer, SALT_V);
        vm.startPrank(attacker);
        vm.expectRevert();
        IPoolManager(address(manager)).initialize(_keyFor(hook), TickMath.getSqrtPriceAtTick(0));
        PoolKey memory foreign = PoolKey(hookArgs.currency0, hookArgs.currency1, 3000, 60, IHooks(hook));
        vm.expectRevert();
        IPoolManager(address(manager)).initialize(foreign, TickMath.getSqrtPriceAtTick(0));
        vm.stopPrank();
        (uint160 sqrtP,,,) = IPoolManager(address(manager)).getSlot0(foreign.toId());
        assertEq(sqrtP, 0);
    }

    // ================================================================== D-3b

    function test_D_3b__onlyTheVaultRegistersArmsAcknowledgesAndRollsTheWitness() public {
        (vault, hook) = _deploy(deployer, SALT_V);
        ITouchstoneHook hk = ITouchstoneHook(hook);
        assertEq(TouchstoneHook(hook).VAULT(), vault);
        vm.startPrank(attacker);
        vm.expectRevert(ITouchstoneHook.OnlyVault.selector);
        hk.setVaultRange(-1000, 1000, 0);
        vm.expectRevert(ITouchstoneHook.OnlyVault.selector);
        hk.armAtSeed();
        vm.expectRevert(ITouchstoneHook.OnlyVault.selector);
        hk.ackBreaches(1);
        vm.expectRevert(ITouchstoneHook.OnlyVault.selector);
        hk.fundWitness(1, 1);
        vm.expectRevert(ITouchstoneHook.OnlyVault.selector);
        hk.rollWitness(100);
        vm.expectRevert(ITouchstoneHook.OnlySelf.selector);
        TouchstoneHook(hook).witnessStep(100);
        vm.stopPrank();
        // the vault's funding must fit the 128-bit reserve (no silent truncation)
        vm.prank(vault);
        vm.expectRevert(ITouchstoneHook.Overflow.selector);
        hk.fundWitness(uint256(type(uint128).max) + 1, 0);
        assertTrue(hk.obs().emptyRange);
        // the vault itself can register
        hk.snapshotIfNewBlock();
        vm.prank(vault);
        hk.armAtSeed();
        vm.prank(vault);
        hk.setVaultRange(-1000, 1000, 0);
        assertFalse(hk.obs().emptyRange);
    }

    // ================================================================== D-5

    function _deployReverts(bytes memory err) internal {
        (, , bytes32 sH) = _predict();
        _fund(deployer);
        vm.expectRevert(err);
        factory.deploy(_vaultCode(), _extCode(), _hookCode(), hookArgs, SALT_V, sH, seedParams);
    }

    function _history(uint256 secondsOfHistory) internal {
        uint32[] memory s = new uint32[](1);
        int24[] memory k = new int24[](1);
        s[0] = uint32(vm.getBlockTimestamp() - secondsOfHistory);
        refPool.setPath(s, k);
    }

    /// A reference with less than GRID_SEC + REF_WINDOW of history fails the deploy; exactly enough succeeds.
    function test_D_5__tooYoungReferenceFailsClosed() public {
        uint256 need = tier.GRID_SEC + tier.REF_WINDOW;
        _history(need - 1);
        _deployReverts(bytes("OLD"));
        _history(need);
        (, hook) = _deploy(deployer, SALT_V);
        assertGt(hook.code.length, 0);
    }

    /// A reference whose observation cardinality is below MIN_CARD fails the deploy; exactly MIN_CARD succeeds.
    function test_D_5__smallCardinalityFailsClosed() public {
        refPool.setCardinality(tier.MIN_CARD - 1);
        _deployReverts(abi.encodeWithSelector(ITouchstoneHook.BadRef.selector));
        refPool.setCardinality(tier.MIN_CARD);
        (, hook) = _deploy(deployer, SALT_V);
        assertGt(hook.code.length, 0);
    }
}
