// SPDX-License-Identifier: MIT
pragma solidity 0.8.26;

import {VectorBase} from "../utils/VectorBase.sol";
import {TickMath} from "@uniswap/v4-core/src/libraries/TickMath.sol";
import {ExecLedger} from "../../src/libraries/ExecLedger.sol";
import {Holdings} from "../../src/types/Holdings.sol";

/// @notice U-3 and U-settle (library half): `ExecLedger.measure` and `ExecLedger.settleExtra` bit for bit against the
///         cases of test/vectors/ledger.json, plus the properties the vectors cannot state on their own.
contract LedgerVectorsTest is VectorBase {
    function _h(string memory c, string memory p) internal pure returns (Holdings memory) {
        return Holdings(
            uint128(_u(c, string.concat(p, ".idle0"))),
            uint128(_u(c, string.concat(p, ".idle1"))),
            uint128(_u(c, string.concat(p, ".L"))),
            int24(_i(c, string.concat(p, ".lo"))),
            int24(_i(c, string.concat(p, ".hi")))
        );
    }

    function _hk(string memory c, string memory p) internal view {
        _keys(c, p, _l5("idle0", "idle1", "L", "lo", "hi"));
    }

    function _measureKeys(string memory c) internal view {
        string[] memory k = new string[](9);
        (k[0], k[1], k[2], k[3], k[4]) = ("before", "after", "sqrtS0", "sqrtSpot", "sqrtRef");
        (k[5], k[6], k[7], k[8]) = ("reserve", "S", "unvested", "bond");
        _keys(c, ".in", k);
        _hk(c, ".in.before");
        _hk(c, ".in.after");
        string[] memory o = new string[](6);
        (o[0], o[1], o[2], o[3], o[4], o[5]) = ("charge", "pStar", "navAfterPStar", "burn", "burnR", "ok");
        _keys(c, ".out", o);
    }

    function _settleKeys(string memory c) internal view {
        string[] memory k = new string[](9);
        (k[0], k[1], k[2], k[3], k[4]) = ("before", "after", "tickMk", "feeTicks", "charge0");
        (k[5], k[6], k[7], k[8]) = ("reserve", "now", "S", "reserveShares");
        _keys(c, ".in", k);
        _hk(c, ".in.before");
        _hk(c, ".in.after");
        _hk(c, ".in.now");
        _keys(c, ".out", _l3("extra", "pMk", "burnS"));
    }

    function _measure(string memory c)
        internal
        pure
        returns (uint256 charge, uint160 pStar, uint256 navA, uint256 burn, uint256 burnR, bool ok)
    {
        return ExecLedger.measure(
            _h(c, ".in.before"),
            _h(c, ".in.after"),
            uint160(_u(c, ".in.sqrtS0")),
            uint160(_u(c, ".in.sqrtSpot")),
            uint160(_u(c, ".in.sqrtRef")),
            _u(c, ".in.reserve"),
            _u(c, ".in.S"),
            _u(c, ".in.unvested"),
            _u(c, ".in.bond")
        );
    }

    /// @notice Every `measure` case: charge, p*, NAV_after(p*), burn, burnR and the gate, bit for bit (incl. a zero
    ///         action, a pushed spot, a lagging reference, the 0 floor, the s0 leg, ties at distinct prices, the reserve
    ///         at the gate's edge and both amounts saturated against NAV_after(p*) = 0).
    function test_U_3__ledgerMeasureVectorsBitForBit() public {
        string[] memory cs = _cases("ledger");
        uint256 n;
        for (uint256 i; i < cs.length; ++i) {
            string memory c = cs[i];
            _keys(c, "$", _l4("id", "fn", "in", "out"));
            cid = _s(c, ".id");
            if (!_eq(_s(c, ".fn"), "ExecLedger.measure")) continue;
            _measureKeys(c);
            (uint256 charge, uint160 pStar, uint256 navA, uint256 burn, uint256 burnR, bool ok) = _measure(c);
            assertEq(charge, _u(c, ".out.charge"), string.concat(cid, " charge"));
            assertEq(uint256(pStar), _u(c, ".out.pStar"), string.concat(cid, " pStar"));
            assertEq(navA, _u(c, ".out.navAfterPStar"), string.concat(cid, " navAfterPStar"));
            assertEq(burn, _u(c, ".out.burn"), string.concat(cid, " burn"));
            assertEq(burnR, _u(c, ".out.burnR"), string.concat(cid, " burnR"));
            assertEq(ok, _b(c, ".out.ok"), string.concat(cid, " ok"));
            ++n;
        }
        assertGe(n, 10, "measure cases");
    }

    /// @notice Every `settleExtra` case: extra, the markout price and the reserve burn, bit for bit (the upper leg
    ///         rounded up, both legs clamped at the tick bounds, the cap by the reserve and by reserveShares, NAV_now = 0).
    function test_U_settle__settleExtraVectorsBitForBit() public {
        string[] memory cs = _cases("ledger");
        uint256 n;
        for (uint256 i; i < cs.length; ++i) {
            string memory c = cs[i];
            cid = _s(c, ".id");
            if (!_eq(_s(c, ".fn"), "ExecLedger.settleExtra")) continue;
            _settleKeys(c);
            (uint256 extra, uint160 pMk, uint256 burnS) = ExecLedger.settleExtra(
                _h(c, ".in.before"),
                _h(c, ".in.after"),
                int24(_i(c, ".in.tickMk")),
                int24(_i(c, ".in.feeTicks")),
                _u(c, ".in.charge0"),
                _u(c, ".in.reserve"),
                _h(c, ".in.now"),
                _u(c, ".in.S"),
                _u(c, ".in.reserveShares")
            );
            assertEq(extra, _u(c, ".out.extra"), string.concat(cid, " extra"));
            assertEq(uint256(pMk), _u(c, ".out.pMk"), string.concat(cid, " pMk"));
            assertEq(burnS, _u(c, ".out.burnS"), string.concat(cid, " burnS"));
            ++n;
        }
        assertGe(n, 10, "settleExtra cases");
    }

    // ------------------------------------------------------------------ properties

    /// @notice The upper markout leg sits one tick above `tickMk + feeTicks` (the floor of the window's mean is
    ///         rounded up): a sale of token0 is priced there, not at `tickMk + feeTicks`.
    function test_U_settle__upperLegIsRoundedUp() public pure {
        Holdings memory b = Holdings(1e21, 1e21, 0, 0, 0);
        Holdings memory a = Holdings(1e21 - 1e18, 1e21 + 1e18, 0, 0, 0); // sold 1 token0 for 1 token1
        (, uint160 pMk,) = ExecLedger.settleExtra(b, a, 100, 5, 0, type(uint128).max, a, 1e24, 1e20);
        assertEq(pMk, TickMath.getSqrtPriceAtTick(106), "upper leg at tickMk + feeTicks + 1");
    }

    /// @notice The settlement never reverts on its legs: a markout tick at either bound settles (clamped legs).
    function testFuzz_U_settle__legsClampedNeverRevert(int24 tickMk, uint8 fee, uint128 res) public pure {
        tickMk = int24(bound(tickMk, TickMath.MIN_TICK, TickMath.MAX_TICK));
        Holdings memory b = Holdings(1e21, 1e21, 0, 0, 0);
        Holdings memory a = Holdings(1e21 - 1e18, 1e21 + 1e17, 0, 0, 0);
        (uint256 extra, uint160 pMk, uint256 burnS) =
            ExecLedger.settleExtra(b, a, tickMk, int24(uint24(fee)), 0, res, a, 1e24, 1e20);
        assertLe(extra, res, "extra <= reserve");
        assertLe(burnS, 1e20, "burnS <= reserveShares");
        assertGe(pMk, TickMath.MIN_SQRT_PRICE, "pMk in range");
        assertLt(pMk, TickMath.MAX_SQRT_PRICE, "pMk in range");
    }

    /// @notice `ok` is computed without overflow when both amounts saturate (NAV_after(p*) = 0, charge and reserve > 0).
    function test_U_3__gateDoesNotOverflowOnSaturatedAmounts() public pure {
        Holdings memory b = Holdings(10, 10, 0, 0, 0);
        Holdings memory a = Holdings(0, 0, 0, 0, 0);
        uint160 p = TickMath.getSqrtPriceAtTick(0);
        (,,, uint256 burn, uint256 burnR, bool ok) =
            ExecLedger.measure(b, a, p, p, p, 1, 1e24, type(uint128).max, type(uint128).max);
        assertEq(burn, type(uint256).max, "burn saturates");
        assertEq(burnR, type(uint256).max, "burnR saturates");
        assertFalse(ok, "a saturated amount fails the gate");
    }

    /// @notice Between actions there is no charge: identical holdings give charge 0 at any three prices.
    function testFuzz_U_3__noActionNoCharge(uint128 x, uint128 y, int24 t0, int24 t1, int24 t2) public pure {
        t0 = int24(bound(t0, -400_000, 400_000));
        t1 = int24(bound(t1, -400_000, 400_000));
        t2 = int24(bound(t2, -400_000, 400_000));
        Holdings memory h = Holdings(x, y, 0, 0, 0);
        (uint256 charge, uint160 pStar,, uint256 burn,,) = ExecLedger.measure(
            h, h, TickMath.getSqrtPriceAtTick(t0), TickMath.getSqrtPriceAtTick(t1), TickMath.getSqrtPriceAtTick(t2), 0, 1e24, 0, 0
        );
        assertEq(charge, 0, "charge 0");
        assertEq(burn, 0, "burn 0");
        assertEq(pStar, TickMath.getSqrtPriceAtTick(t1), "p* = spot on a tie");
    }
}
