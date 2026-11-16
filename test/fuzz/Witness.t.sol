// SPDX-License-Identifier: MIT
pragma solidity 0.8.26;

import {IPoolManager} from "@uniswap/v4-core/src/interfaces/IPoolManager.sol";
import {StateLibrary} from "@uniswap/v4-core/src/libraries/StateLibrary.sol";
import {ModifyLiquidityParams} from "@uniswap/v4-core/src/types/PoolOperation.sol";
import {ITouchstoneHook} from "../../src/interfaces/ITouchstoneHook.sol";
import {RealPoolBase} from "../utils/RealPoolBase.sol";

/// @notice The witness: the hook's passive-mandate position, rolled once per epoch inside the vault's unlock.
contract WitnessFuzzTest is RealPoolBase {
    using StateLibrary for IPoolManager;

    bytes32 internal constant REF_SALT = keccak256("reference position");

    function setUp() public {
        _setUpPool();
    }

    function _toEpochStart() internal returns (uint32 e) {
        uint256 b = vm.getBlockNumber();
        uint256 next = (b / tier.EPOCH + 1) * tier.EPOCH;
        advance(next - b);
        hook.refreshRef(); // the epoch's first cache write sets epochStartRef
        e = uint32(next / tier.EPOCH);
    }

    function _pm() internal view returns (IPoolManager) {
        return IPoolManager(address(manager));
    }

    /// The fee leg the witness reports equals, to the wei, the fees a real 1e18-liquidity position opened on the same
    /// range in the same block collects over the epoch.
    function test_F_4__witnessFeeLegEqualsA1e18ReferencePosition(uint256 seed, uint8 nSwaps) public {
        uint32 e1 = _toEpochStart();
        (uint256 fg0, uint256 fg1, uint128 lw, bool fb) = vault.roll(e1);
        assertEq(fg0 + fg1, 0);
        assertEq(lw, tier.L_W_TARGET);
        assertFalse(fb);
        (int24 lo, int24 hi,,,,) = hook.witness();
        assertEq(lo, -1000);
        assertEq(hi, 1000);
        // the position lives under the per-epoch salt
        (uint128 liq,,) = _pm().getPositionInfo(key.toId(), address(hook), lo, hi, keccak256(abi.encode("witness", e1)));
        assertEq(liq, lw);
        lpRouter.modifyLiquidity(key, ModifyLiquidityParams(lo, hi, 1e18, REF_SALT), "");

        uint256 n = bound(nSwaps, 1, 8);
        for (uint256 i; i < n; ++i) {
            seed = uint256(keccak256(abi.encode(seed, i)));
            advance(1 + seed % 7);
            (, int24 t,,) = _pm().getSlot0(key.toId());
            // keep the price near the witness: swap toward the centre, sometimes across it
            _swap(seed % 4 == 0 ? t < 0 : t >= 0, bound(seed >> 8, 1e15, 5e19));
        }
        uint32 e2 = _toEpochStart();
        (fg0, fg1, lw, fb) = vault.roll(e2);
        uint256 b0 = token0.balanceOf(address(this));
        uint256 b1 = token1.balanceOf(address(this));
        lpRouter.modifyLiquidity(key, ModifyLiquidityParams(lo, hi, 0, REF_SALT), ""); // collect
        assertEq(fg0, token0.balanceOf(address(this)) - b0, "token0 fee leg");
        assertEq(fg1, token1.balanceOf(address(this)) - b1, "token1 fee leg");
        assertGt(fg0 + fg1, 0);
        // the tracked reserve is the hook's claim balance
        (,,,, uint128 r0, uint128 r1) = hook.witness();
        assertEq(_pm().balanceOf(address(hook), key.currency0.toId()), r0);
        assertEq(_pm().balanceOf(address(hook), key.currency1.toId()), r1);
    }

    /// Without a refresh in the epoch the witness is centred on the previous epochStartRef and flagged.
    function test_F_4__witnessFallsBackToThePreviousEpochStartRef() public {
        uint256 b = vm.getBlockNumber();
        advance((b / tier.EPOCH + 1) * tier.EPOCH - b);
        uint32 e = uint32(vm.getBlockNumber() / tier.EPOCH);
        (,, uint128 lw, bool fb) = vault.roll(e);
        assertTrue(fb);
        assertTrue(hook.witnessFallback());
        assertEq(lw, tier.L_W_TARGET);
        // a call for an epoch already rolled is ignored
        (,, lw,) = vault.roll(e);
        assertEq(lw, 0);
    }

    /// A roll that cannot execute (here: outside the PoolManager's unlock) leaves the witness as it was and does not
    /// revert; the zero liquidity it returns is recorded as witnessMissing.
    function test_F_4__failedRollIsFailSoft() public {
        uint32 e1 = _toEpochStart();
        vault.roll(e1);
        (int24 lo, int24 hi, uint32 we, uint128 l,,) = hook.witness();
        bool fbBefore = hook.witnessFallback(); // e1 had a refresh: not a fallback
        // the next epoch without a refresh: its roll would be a fallback
        uint256 b = vm.getBlockNumber();
        advance((b / tier.EPOCH + 1) * tier.EPOCH - b);
        uint32 e2 = uint32(vm.getBlockNumber() / tier.EPOCH);
        vm.prank(address(vault));
        (uint256 fg0, uint256 fg1, uint128 lw, bool fbReturned) = hook.rollWitness(e2);
        assertEq(fg0 + fg1 + lw, 0);
        (int24 lo2, int24 hi2, uint32 we2, uint128 l2,,) = hook.witness();
        assertEq(abi.encode(lo2, hi2, we2, l2), abi.encode(lo, hi, we, l));
        // the fallback flag still describes the unchanged witness (the failed roll would have set it differently)
        assertTrue(fbReturned != fbBefore, "control: the failed roll's own fallback differs from the live one");
        assertEq(hook.witnessFallback(), fbBefore);
        // only the vault rolls, and only the hook runs the roll body
        vm.expectRevert(ITouchstoneHook.OnlyVault.selector);
        hook.rollWitness(e2 + 1);
        vm.expectRevert(ITouchstoneHook.OnlySelf.selector);
        hook.witnessStep(e2 + 1);
    }
}
