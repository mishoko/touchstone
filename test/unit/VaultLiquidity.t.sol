// SPDX-License-Identifier: MIT
pragma solidity 0.8.26;

import {FullMath} from "@uniswap/v4-core/src/libraries/FullMath.sol";
import {LiquidPoolBase} from "../utils/LiquidPoolBase.sol";
import {ExpectedFlows} from "../utils/ExpectedFlows.sol";
import {MockRefPool} from "../utils/MockRefPool.sol";
import {Holdings} from "../../src/types/Holdings.sol";
import {Ladder} from "../../src/libraries/Ladder.sol";
import {IPoolManager} from "@uniswap/v4-core/src/interfaces/IPoolManager.sol";
import {StateLibrary} from "@uniswap/v4-core/src/libraries/StateLibrary.sol";

/// @notice Pro-rata flows with a live position (L > 0, placed through the test-only seam), checked against the
///         PoolManager's own state and math: collect-first, the L leg rounded up on deposit and down on withdrawal,
///         and withdrawals under every failure mode of the reference.
abstract contract VaultLiquidityTests is LiquidPoolBase {
    int24 internal lo;
    int24 internal hi;

    function setUp() public virtual override {
        super.setUp();
        int24 c = _seedTick();
        (lo, hi) = (c - 600, c + 600);
        hv.placeForTest(lo, hi);
        assertGt(hv.holdings().L, 0, "placed");
    }

    /// @dev Third-party round trips across the vault's range, so its position earns fees in both tokens.
    function _churn() internal {
        Holdings memory h = hv.holdings();
        for (uint256 i; i < 4; ++i) {
            _swap(true, uint256(h.idle0) * 50 + 1e6, 300);
            _swap(false, uint256(h.idle1) * 50 + 1e6, 300);
        }
        advance(1);
    }

    function test_I_2__withdrawPaysTheExactShareOfEveryBucketAfterFees() public {
        _churn();
        uint256 s = v.balanceOf(alice) / 3 + 7; // an odd share: every bucket rounds
        (uint256 e0, uint256 e1, uint128 dL) = _expectWithdraw(s);
        Holdings memory h0 = hv.holdings();
        assertGt(dL, 0, "the L leg is exercised");
        uint256 b0 = token0.balanceOf(alice);
        uint256 b1 = token1.balanceOf(alice);
        (uint256 a0, uint256 a1) = _withdraw(alice, s);
        assertEq(token0.balanceOf(alice) - b0, e0, "token0 paid: floor of the idle share plus the rounded-down L leg");
        assertEq(token1.balanceOf(alice) - b1, e1, "token1 paid");
        assertEq(a0, e0, "returned a0");
        assertEq(a1, e1, "returned a1");
        assertEq(h0.L - hv.holdings().L, dL, "liquidity removed = floor(L*s/S_pre)");
    }

    function test_U_5__depositChargesTheLLegRoundedUp() public {
        _churn();
        uint256 s = 1e21 + 7;
        (uint256 f0, uint256 f1) = ExpectedFlows.fees(manager, poolId, vault, lo, hi);
        Holdings memory h = hv.holdings();
        uint256 S = v.totalSupply();
        uint128 dL = uint128(FullMath.mulDivRoundingUp(h.L, s, S));
        assertTrue(mulmod(h.L, s, S) != 0, "the L leg rounds");
        (uint256 l0, uint256 l1) = ExpectedFlows.legs(manager, poolId, lo, hi, dL, true);
        uint256 e0 = FullMath.mulDivRoundingUp(h.idle0 + f0, s, S) + l0;
        uint256 e1 = FullMath.mulDivRoundingUp(h.idle1 + f1, s, S) + l1;
        uint256 b0 = token0.balanceOf(bob);
        uint256 b1 = token1.balanceOf(bob);
        _deposit(bob, s);
        assertEq(b0 - token0.balanceOf(bob), e0, "token0 charged: ceil of the idle share plus the rounded-up L leg");
        assertEq(b1 - token1.balanceOf(bob), e1, "token1 charged");
        assertEq(hv.holdings().L - h.L, dL, "liquidity added = ceil(L*s/S)");
    }

    function test_I_1__claimsAndPositionCoverTheLedgerAfterFlowsAndFees() public {
        _churn();
        _deposit(bob, 3e21 + 1);
        _churn();
        _withdraw(alice, 1e22 + 3);
        _churn();
        _withdraw(bob, v.balanceOf(bob));
        Holdings memory h = hv.holdings();
        (uint128 liq,,) = _positionInfo(h.lo, h.hi);
        assertEq(liq, h.L, "the position is exactly the ledger's L");
        assertGe(manager.balanceOf(vault, token0Id()), h.idle0, "claims0 >= idle0");
        assertGe(manager.balanceOf(vault, token1Id()), h.idle1, "claims1 >= idle1");
    }

    function test_I_10__withdrawSucceedsInEveryReferenceFailureMode() public {
        MockRefPool.Mode[5] memory modes = [
            MockRefPool.Mode.Old,
            MockRefPool.Mode.Revert,
            MockRefPool.Mode.Short,
            MockRefPool.Mode.GasBomb,
            MockRefPool.Mode.ReturnBomb
        ];
        for (uint256 i; i < modes.length; ++i) {
            refPool.setMode(modes[i]);
            advance(25); // a new anchor: the snapshot tries to refresh
            _withdraw(alice, 1e20);
            _deposit(bob, 1e20);
        }
        refPool.setMode(MockRefPool.Mode.Ok);
        // out of range until removal, then withdraw in EXITED with the reference broken
        refPool.setTick(_seedTick() + 5000);
        _driveBreaches(3);
        refPool.setMode(MockRefPool.Mode.Revert);
        advance(25);
        _withdraw(alice, v.balanceOf(alice));
        (,,,,, uint8 mode,) = v.bank();
        assertEq(mode, Ladder.EXITED, "exited");
        assertEq(v.balanceOf(alice), 0, "fully withdrawn in EXITED with the reference unreadable");
    }

    // ------------------------------------------------------------------ helpers

    function _expectWithdraw(uint256 s) internal view returns (uint256 e0, uint256 e1, uint128 dL) {
        (uint256 f0, uint256 f1) = ExpectedFlows.fees(manager, poolId, vault, lo, hi);
        Holdings memory h = hv.holdings();
        uint256 S = v.totalSupply();
        dL = uint128(FullMath.mulDiv(h.L, s, S));
        (uint256 r0, uint256 r1) = ExpectedFlows.legs(manager, poolId, lo, hi, dL, false);
        e0 = FullMath.mulDiv(h.idle0 + f0, s, S) + r0;
        e1 = FullMath.mulDiv(h.idle1 + f1, s, S) + r1;
    }

    function _positionInfo(int24 l, int24 u) internal view returns (uint128 liq, uint256 g0, uint256 g1) {
        return StateLibrary.getPositionInfo(IPoolManager(address(manager)), poolId, vault, l, u, bytes32(0));
    }

    function token0Id() internal view returns (uint256) {
        return uint256(uint160(address(token0)));
    }

    function token1Id() internal view returns (uint256) {
        return uint256(uint160(address(token1)));
    }
}

contract VaultLiquidityTick0Test is VaultLiquidityTests {}

contract VaultLiquidityTick196kTest is VaultLiquidityTests {
    function _highTick() internal pure override returns (bool) {
        return true;
    }
}
