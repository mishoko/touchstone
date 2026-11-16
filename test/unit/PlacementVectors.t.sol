// SPDX-License-Identifier: MIT
pragma solidity 0.8.26;

import {VectorBase} from "../utils/VectorBase.sol";
import {TickMath} from "@uniswap/v4-core/src/libraries/TickMath.sol";
import {ExecLedger} from "../../src/libraries/ExecLedger.sol";
import {Placement} from "../../src/libraries/Placement.sol";
import {Holdings} from "../../src/types/Holdings.sol";

/// @notice U-guard (library half): `Placement.check` and `Placement.size` (reached through `ExecLedger.placementSize`,
///         as the vault calls it) bit for bit against test/vectors/placement.json.
contract PlacementVectorsTest is VectorBase {
    function _h(string memory c, string memory p) internal pure returns (Holdings memory) {
        return Holdings(
            uint128(_u(c, string.concat(p, ".idle0"))),
            uint128(_u(c, string.concat(p, ".idle1"))),
            uint128(_u(c, string.concat(p, ".L"))),
            int24(_i(c, string.concat(p, ".lo"))),
            int24(_i(c, string.concat(p, ".hi")))
        );
    }

    /// @notice Every `check` case: the first failing code in numeric order (fence, width, alignment, spot, reference).
    function test_U_guard__placementCheckVectorsBitForBit() public {
        string[] memory cs = _cases("placement");
        uint256 n;
        for (uint256 i; i < cs.length; ++i) {
            string memory c = cs[i];
            _keys(c, "$", _l4("id", "fn", "in", "out"));
            cid = _s(c, ".id");
            if (!_eq(_s(c, ".fn"), "Placement.check")) continue;
            string[] memory k = new string[](7);
            (k[0], k[1], k[2], k[3], k[4], k[5], k[6]) = ("lo", "hi", "tickSpot", "refTick", "band", "wTicks", "spacing");
            _keys(c, ".in", k);
            _keys(c, ".out", _l1("errCode"));
            uint8 code = Placement.check(
                int24(_i(c, ".in.lo")),
                int24(_i(c, ".in.hi")),
                int24(_i(c, ".in.tickSpot")),
                int24(_i(c, ".in.refTick")),
                int24(_i(c, ".in.band")),
                int24(_i(c, ".in.wTicks")),
                int24(_i(c, ".in.spacing"))
            );
            assertEq(code, _u(c, ".out.errCode"), string.concat(cid, " errCode"));
            ++n;
        }
        assertGe(n, 10, "check cases");
    }

    /// @notice Every `size` case: L with headroom, the holdings after the add (rounded up) and δ_after at the
    ///         reference, incl. the saturated legs next to an edge and at an extreme price, and the DELTA_MIN boundary.
    function test_U_guard__placementSizeVectorsBitForBit() public {
        string[] memory cs = _cases("placement");
        uint256 n;
        for (uint256 i; i < cs.length; ++i) {
            string memory c = cs[i];
            cid = _s(c, ".id");
            if (!_eq(_s(c, ".fn"), "Placement.size")) continue;
            _keys(c, ".in", _l5("idle", "lo", "hi", "sqrtSpot", "sqrtRef"));
            _keys(c, ".in.idle", _l5("idle0", "idle1", "L", "lo", "hi"));
            _keys(c, ".out", _l3("L", "after", "deltaAfter"));
            _keys(c, ".out.after", _l5("idle0", "idle1", "L", "lo", "hi"));
            (uint128 L, Holdings memory a, uint64 d) = ExecLedger.placementSize(
                _h(c, ".in.idle"),
                int24(_i(c, ".in.lo")),
                int24(_i(c, ".in.hi")),
                uint160(_u(c, ".in.sqrtSpot")),
                uint160(_u(c, ".in.sqrtRef"))
            );
            assertEq(L, _u(c, ".out.L"), string.concat(cid, " L"));
            assertEq(a.idle0, _u(c, ".out.after.idle0"), string.concat(cid, " after.idle0"));
            assertEq(a.idle1, _u(c, ".out.after.idle1"), string.concat(cid, " after.idle1"));
            assertEq(a.L, _u(c, ".out.after.L"), string.concat(cid, " after.L"));
            assertEq(int256(a.lo), _i(c, ".out.after.lo"), string.concat(cid, " after.lo"));
            assertEq(int256(a.hi), _i(c, ".out.after.hi"), string.concat(cid, " after.hi"));
            assertEq(d, _u(c, ".out.deltaAfter"), string.concat(cid, " deltaAfter"));
            ++n;
        }
        assertGe(n, 10, "size cases");
    }

    /// @notice `size` never reverts on a leg overflow: with spot one unit inside either edge at any range, the legs
    ///         saturate and the amounts consumed never exceed idle.
    function testFuzz_U_guard__sizeLegsSaturateNeverRevert(int24 c, uint112 x, uint112 y, bool upper) public pure {
        c = int24(bound(c, TickMath.MIN_TICK + 10, TickMath.MAX_TICK - 2010)) / 10 * 10;
        (int24 lo, int24 hi) = (c, c + 2000);
        uint160 sp = upper ? TickMath.getSqrtPriceAtTick(hi) - 1 : TickMath.getSqrtPriceAtTick(lo) + 1;
        (uint128 L, Holdings memory a,) = ExecLedger.placementSize(Holdings(x, y, 0, 0, 0), lo, hi, sp, sp);
        assertLe(a.idle0, x, "idle0 never grows");
        assertLe(a.idle1, y, "idle1 never grows");
        assertEq(a.L, L, "after carries L");
    }
}
