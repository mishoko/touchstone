// SPDX-License-Identifier: MIT
pragma solidity 0.8.26;

import {IERC20} from "@openzeppelin/contracts/token/ERC20/IERC20.sol";
import {IPoolManager} from "@uniswap/v4-core/src/interfaces/IPoolManager.sol";
import {PoolId} from "@uniswap/v4-core/src/types/PoolId.sol";
import {PoolKey} from "@uniswap/v4-core/src/types/PoolKey.sol";
import {IHooks} from "@uniswap/v4-core/src/interfaces/IHooks.sol";
import {StateLibrary} from "@uniswap/v4-core/src/libraries/StateLibrary.sol";
import {TickMath} from "@uniswap/v4-core/src/libraries/TickMath.sol";
import {VaultBase} from "../utils/VaultBase.sol";
import {VaultView} from "../utils/VaultView.sol";
import {HookMining} from "../../script/Mine.s.sol";
import {CuratorBundler, DeployArgs} from "../../src/periphery/CuratorBundler.sol";
import {TouchstoneVault} from "../../src/TouchstoneVault.sol";
import {HookArgs, SeedParams} from "../../src/interfaces/ITouchstoneFactory.sol";
import {Holdings} from "../../src/types/Holdings.sol";
import {PreSwap} from "../../src/types/PreSwap.sol";

using VaultView for TouchstoneVault;

/// @notice The curator's `CuratorBundler`: the deployment with the first placement in one transaction, the spot
///         re-alignment before a placement, the owner restriction on every function and the PoolManager-only callback.
contract CuratorBundlerTest is VaultBase {
    using StateLibrary for *;

    address internal owner = makeAddr("owner");
    CuratorBundler internal bundler;
    DeployArgs internal args;

    function setUp() public override {
        _setUpEnv();
        bundler = new CuratorBundler(owner, factory);
        address predicted = HookMining.vaultAddress(address(factory), address(bundler), SALT_V, _vaultCode());
        HookArgs memory ha = _hookArgsFor(predicted);
        (bytes32 sH,) = HookMining.mine(address(factory), address(bundler), _hookCode(), ha, 0);
        SeedParams memory sp = SeedParams({a0: 1e18, a1: 1e18, curator: address(bundler), wit0: 1e9, wit1: 1e9});
        args = DeployArgs(_vaultCode(), _hookCode(), ha, SALT_V, sH, sp);
        token0.mint(address(bundler), 1e30); // the seed and the spot moves
        token1.mint(address(bundler), 1e30);
    }

    function _none() internal pure returns (PreSwap memory) {
        return PreSwap(false, false, 0);
    }

    function _setRef(int24 t) internal {
        uint32[] memory s = new uint32[](1);
        int24[] memory k = new int24[](1);
        (s[0], k[0]) = (1, t);
        refPool.setPath(s, k);
    }

    function _deployAndPlace(int24 lo, int24 hi) internal {
        vm.prank(owner);
        (vault, hook) = bundler.deployAndPlace(args, lo, hi, _none(), type(uint256).max);
        v = TouchstoneVault(vault);
    }

    function _poolId() internal view returns (PoolId) {
        return PoolId.wrap(keccak256(abi.encode(PoolKey(args.hookArgs.currency0, args.hookArgs.currency1, FEE, TICK_SPACING, IHooks(hook)))));
    }

    function _sqrtSpot() internal view returns (uint160 sp) {
        (sp,,,) = manager.getSlot0(_poolId());
    }

    /// @notice The first placement at deploy, the seed price `REF_BAND` from the reference: the bundler is the salt
    ///         sender, the seed payer and the curator; with no unvested pay the charge and the reserve come from the
    ///         bond (`reserveBond = burnR`); deposits revert until the bundler's `settle()` after the markout window.
    function test_U_solv__firstPlacementAtDeployThroughTheBundler() public {
        _setRef(tier.REF_BAND);
        _deployAndPlace(-940, 1060);
        assertEq(v.curator(), address(bundler), "the bundler is the curator");
        Holdings memory h = v.holdings();
        assertGt(h.L, 0, "placed in the deployment transaction");
        assertEq(h.lo, -940, "lo");
        (,,, uint128 bond,, uint8 mode,) = v.bank();
        assertEq(mode, 0, "NORMAL");
        (uint40 tA,,, uint128 c0, uint128 r) = v.pending();
        assertEq(tA, vm.getBlockTimestamp(), "pending");
        assertGt(r, 0, "a reserve");
        c0;
        // the layout snapshot: slot 19 holds placedByCurator (byte 5) and reserveShares (bytes 11..26); slot 20 reserveBond
        uint256 w19 = uint256(vm.load(vault, bytes32(uint256(19))));
        uint256 w20 = uint256(vm.load(vault, bytes32(uint256(20))));
        assertEq((w19 >> 40) & 1, 1, "placedByCurator");
        assertGt(uint128(w19 >> 88), 0, "the reserve's shares");
        assertEq(uint128(w19 >> 88), uint128(w20), "reserveBond = burnR (all from the bond)");
        assertLt(bond, v.totalSupply(), "bond");
        _fundUser(alice, 1e30);
        vm.prank(alice);
        vm.expectRevert(TouchstoneVault.MarkoutPending.selector);
        v.deposit(1e18, type(uint256).max, type(uint256).max, alice);
        advance(2 + tier.H_MARKOUT / blocktime);
        vm.prank(owner);
        bundler.settle();
        (tA,,,,) = v.pending();
        assertEq(tA, 0, "settled by the bundler");
        _deposit(alice, 1e18);
    }

    /// @notice `moveAndPlace` re-aligns spot to the reference price in the bundler's own unlock, then places in the
    ///         same transaction; spot more than `tol` ticks from the alignment price reverts `Tolerance` (boundary ±1).
    function test_U_6__moveAndPlaceReAlignsWithinTheTolerance() public {
        _deployAndPlace(-1000, 1000);
        advance(tier.COOLDOWN);
        _setRef(25);
        uint160 align = TickMath.getSqrtPriceAtTick(25);
        // spot 0 is 25 ticks from the alignment price: a tolerance of 24 reverts, 25 passes
        vm.prank(owner);
        vm.expectRevert(CuratorBundler.Tolerance.selector);
        bundler.moveAndPlace(align, 24, -980, 1020, _none(), 0, type(uint256).max);
        vm.prank(owner);
        bundler.moveAndPlace(align, 25, -980, 1020, _none(), 0, type(uint256).max);
        assertEq(_sqrtSpot(), align, "re-aligned");
        assertEq(v.holdings().lo, -980, "placed");
        (uint40 tA,,,,) = v.pending();
        assertEq(tA, vm.getBlockTimestamp(), "in the same transaction");
    }

    /// @notice `moveSpot` moves the pool's spot with the bundler's tokens; `sweep` returns them to any address.
    function test_U_6__moveSpotAndSweep() public {
        _deployAndPlace(-1000, 1000);
        vm.prank(owner);
        bundler.moveSpot(TickMath.getSqrtPriceAtTick(-30));
        assertEq(_sqrtSpot(), TickMath.getSqrtPriceAtTick(-30), "moved");
        uint256 b = token0.balanceOf(address(bundler));
        vm.prank(owner);
        bundler.sweep(IERC20(address(token0)), owner);
        assertEq(token0.balanceOf(owner), b, "swept");
        assertEq(token0.balanceOf(address(bundler)), 0, "nothing left");
    }

    /// @notice Every bundler function reverts `OnlyOwner` from any other caller.
    function test_U_6__everyBundlerFunctionIsOwnerOnly() public {
        address x = makeAddr("stranger");
        bytes[] memory calls = new bytes[](13);
        calls[0] = abi.encodeCall(CuratorBundler.deployAndPlace, (args, -1000, 1000, _none(), 0));
        calls[1] = abi.encodeCall(CuratorBundler.moveSpot, (uint160(1 << 96)));
        calls[2] = abi.encodeCall(CuratorBundler.moveAndPlace, (uint160(1 << 96), 0, -1000, 1000, _none(), 0, 0));
        calls[3] = abi.encodeCall(CuratorBundler.setPositions, (-1000, 1000, _none(), 0, 0));
        calls[4] = abi.encodeCall(CuratorBundler.rebalanceSwap, (true, 1, 0));
        calls[5] = abi.encodeCall(CuratorBundler.settle, ());
        calls[6] = abi.encodeCall(CuratorBundler.retire, ());
        calls[7] = abi.encodeCall(CuratorBundler.topUpBond, (1));
        calls[8] = abi.encodeCall(CuratorBundler.releaseBond, ());
        calls[9] = abi.encodeCall(CuratorBundler.claim, ());
        calls[10] = abi.encodeCall(CuratorBundler.refreshRef, ());
        calls[11] = abi.encodeCall(CuratorBundler.poke, ());
        calls[12] = abi.encodeCall(CuratorBundler.sweep, (IERC20(address(token0)), x));
        for (uint256 i; i < calls.length; ++i) {
            vm.prank(x);
            (bool ok, bytes memory ret) = address(bundler).call(calls[i]);
            assertFalse(ok, string.concat("call ", vm.toString(i), " passed"));
            assertEq(bytes4(ret), CuratorBundler.OnlyOwner.selector, string.concat("call ", vm.toString(i)));
        }
        // control: the owner passes the guard (sweep has no other precondition)
        vm.prank(owner);
        bundler.sweep(IERC20(address(token0)), owner);
    }

    /// @notice The bundler's `unlockCallback` accepts the PoolManager only.
    function test_U_6__bundlerUnlockCallbackOnlyFromThePoolManager() public {
        _deployAndPlace(-1000, 1000);
        vm.prank(owner);
        vm.expectRevert(CuratorBundler.OnlyPoolManager.selector);
        bundler.unlockCallback(abi.encode(TickMath.getSqrtPriceAtTick(-30)));
    }

    /// @notice A second deployment through the same bundler reverts `Deployed`.
    function test_U_6__bundlerDeploysOnce() public {
        _deployAndPlace(-1000, 1000);
        vm.prank(owner);
        vm.expectRevert(CuratorBundler.Deployed.selector);
        bundler.deployAndPlace(args, -1000, 1000, _none(), type(uint256).max);
    }
}
