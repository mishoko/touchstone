// SPDX-License-Identifier: MIT
pragma solidity 0.8.26;

import {StateLibrary} from "@uniswap/v4-core/src/libraries/StateLibrary.sol";
import {FullMath} from "@uniswap/v4-core/src/libraries/FullMath.sol";
import {ModifyLiquidityParams} from "@uniswap/v4-core/src/types/PoolOperation.sol";
import {CellBase} from "../manipulation/CellBase.sol";
import {ExecLedger} from "../../src/libraries/ExecLedger.sol";
import {LiquidityValue} from "../../src/libraries/LiquidityValue.sol";
import {Holdings} from "../../src/types/Holdings.sol";

/// @notice U-settle (s15): the settlement's arbitrage fee term `F` against v4's realised fee accrual. A lone
///         third-party position `L` on `[lo, hi)` is added at spot `s`; an arbitrage carries spot to `p`; the fee the
///         position accrued (fee growth inside × L), valued at `p`, lies between the before holding's `F` (every rounding
///         up) and the after holding's credit (every rounding down, less both margins): both directions, in range,
///         partial and one-tick overlaps, no overlap; and with a 1,000-pip protocol fee on (the LP then earns slightly more, by v4's truncation of the composed fee), the bracket still holds.
contract ArbFeeTest is CellBase {
    using StateLibrary for *;

    uint128 internal constant LP = 1e21;

    /// @dev The fee the lone position accrues on the path s → p, valued at p (the state is restored after).
    function _accrued(int24 lo, int24 hi, int24 s, int24 p) internal returns (uint256 v) {
        uint256 snap = vm.snapshotState();
        _thirdPartyPush(_sqrt(s));
        lpRouter.modifyLiquidity(key, ModifyLiquidityParams(lo, hi, int256(uint256(LP)), bytes32(uint256(7))), "");
        (uint256 g0a, uint256 g1a) = manager.getFeeGrowthInside(poolId, lo, hi);
        _thirdPartyPush(_sqrt(p));
        (uint256 g0b, uint256 g1b) = manager.getFeeGrowthInside(poolId, lo, hi);
        uint256 f0;
        uint256 f1;
        unchecked {
            f0 = FullMath.mulDiv(g0b - g0a, LP, 1 << 128);
            f1 = FullMath.mulDiv(g1b - g1a, LP, 1 << 128);
        }
        v = LiquidityValue.val(f0, f1, _sqrt(p));
        vm.revertToState(snap);
    }

    function _bracket(int24 lo, int24 hi, int24 s, int24 p) internal returns (uint256 got) {
        got = _accrued(lo, hi, s, p);
        Holdings memory h = Holdings(0, 0, LP, lo, hi);
        uint256 up = ExecLedger._arbFee(h, _sqrt(s), _sqrt(p), true, FEE);
        uint256 dn = ExecLedger._arbFee(h, _sqrt(s), _sqrt(p), false, FEE);
        assertGe(up, got, "F(before) >= the accrued fee");
        assertLe(dn, got, "the accrued fee >= the credit");
        if (got > 0) assertGt(dn, 0, "non-vacuity: the credit is positive where a fee accrued");
    }

    function test_U_settle__arbFeeBracketsV4Accrual() public {
        int24[4][7] memory cs = [
            [int24(-1000), 1000, 60, -110], // down, in range
            [int24(-1000), 1000, -50, 170], // up, in range
            [int24(-60), 1940, 60, -110], // down, leaves the range at its lower edge
            [int24(-2000), 10, -40, 300], // up, leaves the range at its upper edge
            [int24(-60), 1940, -50, -300], // down, a one-tick overlap
            [int24(200), 2200, 60, -110], // no overlap
            [int24(-10), 10, 300, -300] // through a narrow range
        ];
        uint256 positive;
        for (uint256 i; i < 7; ++i) {
            if (_bracket(cs[i][0], cs[i][1], cs[i][2], cs[i][3]) > 0) ++positive;
        }
        assertGe(positive, 6, "non-vacuity: fees accrue on six of the paths");
    }

    function test_U_settle__arbFeeBracketHoldsWithAProtocolFee() public {
        uint256 off = _bracket(-1000, 1000, 60, -110);
        uint256 offUp = _bracket(-1000, 1000, -50, 170);
        manager.setProtocolFeeController(address(this));
        manager.setProtocolFee(key, uint24(1000) | (uint24(1000) << 12));
        uint256 on = _bracket(-1000, 1000, 60, -110);
        uint256 onUp = _bracket(-1000, 1000, -50, 170);
        // v4 truncates protocolFee·lpFee/10⁶ in the composed swap fee: the LP earns slightly more (≈ +0.1% at 1,000 and
        // 500 pips), which the before term's 2-pip margin covers (the bracket above holds with the fee on)
        assertGe(on, off, "the protocol fee does not lower the LP's fee (down)");
        assertGe(onUp, offUp, "the protocol fee does not lower the LP's fee (up)");
        assertLe(on - off, off / 500, "by at most the truncation (down)");
        assertLe(onUp - offUp, offUp / 500, "by at most the truncation (up)");
    }
}
