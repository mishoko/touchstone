// SPDX-License-Identifier: MIT
pragma solidity 0.8.26;

import {VectorBase} from "../utils/VectorBase.sol";
import {TickMath} from "@uniswap/v4-core/src/libraries/TickMath.sol";
import {SqrtPriceMath} from "@uniswap/v4-core/src/libraries/SqrtPriceMath.sol";
import {FullMath} from "@uniswap/v4-core/src/libraries/FullMath.sol";
import {SafeCast} from "@uniswap/v4-core/src/libraries/SafeCast.sol";
import {LiquidityAmounts} from "@uniswap/v4-periphery/src/libraries/LiquidityAmounts.sol";
import {LiquidityValue} from "../../src/libraries/LiquidityValue.sol";
import {ExecLedger} from "../../src/libraries/ExecLedger.sol";
import {Holdings} from "../../src/types/Holdings.sol";

/// @notice U-2: valuation and the ported math, bit for bit against test/vectors/math.json and the `ExecLedger.value`
///         cases of test/vectors/ledger.json.
contract MathVectorsTest is VectorBase {
    // ------------------------------------------------------------------ external wrappers (to catch reverts)
    function sqrtAt(int24 t) external pure returns (uint160) {
        return TickMath.getSqrtPriceAtTick(t);
    }

    function tickAt(uint160 s) external pure returns (int24) {
        return TickMath.getTickAtSqrtPrice(s);
    }

    function amt0(uint160 a, uint160 b, uint128 l, bool up) external pure returns (uint256) {
        return SqrtPriceMath.getAmount0Delta(a, b, l, up);
    }

    function amt1(uint160 a, uint160 b, uint128 l, bool up) external pure returns (uint256) {
        return SqrtPriceMath.getAmount1Delta(a, b, l, up);
    }

    function liqFor(uint160 s, uint160 a, uint160 b, uint256 x, uint256 y) external pure returns (uint128) {
        return LiquidityAmounts.getLiquidityForAmounts(s, a, b, x, y);
    }

    function mulDiv(uint256 a, uint256 b, uint256 d, bool up) external pure returns (uint256) {
        return up ? FullMath.mulDivRoundingUp(a, b, d) : FullMath.mulDiv(a, b, d);
    }

    function test_U_2__mathVectorsBitForBit() public {
        string[] memory cs = _cases("math");
        assertGe(cs.length, 40, "minimum math cases");
        for (uint256 i; i < cs.length; ++i) {
            string memory c = cs[i];
            _keys(c, "$", _l4("id", "fn", "in", "out"));
            cid = _s(c, ".id");
            _runMath(c, _s(c, ".fn"));
        }
    }

    function _runMath(string memory c, string memory fn) internal {
        bytes memory call;
        string[] memory ins;
        string memory outKey;
        if (_eq(fn, "TickMath.getSqrtPriceAtTick")) {
            ins = _l1("tick");
            outKey = "sqrtPriceX96";
            call = abi.encodeCall(this.sqrtAt, (int24(_i(c, ".in.tick"))));
        } else if (_eq(fn, "TickMath.getTickAtSqrtPrice")) {
            ins = _l1("sqrtPriceX96");
            outKey = "tick";
            call = abi.encodeCall(this.tickAt, (uint160(_u(c, ".in.sqrtPriceX96"))));
        } else if (_eq(fn, "SqrtPriceMath.getAmount0Delta") || _eq(fn, "SqrtPriceMath.getAmount1Delta")) {
            ins = _l4("sqrtPriceAX96", "sqrtPriceBX96", "liquidity", "roundUp");
            outKey = "amount";
            (uint160 a, uint160 b, uint128 l, bool up) = (
                uint160(_u(c, ".in.sqrtPriceAX96")),
                uint160(_u(c, ".in.sqrtPriceBX96")),
                uint128(_u(c, ".in.liquidity")),
                _b(c, ".in.roundUp")
            );
            call = _eq(fn, "SqrtPriceMath.getAmount0Delta")
                ? abi.encodeCall(this.amt0, (a, b, l, up))
                : abi.encodeCall(this.amt1, (a, b, l, up));
        } else if (_eq(fn, "LiquidityAmounts.getLiquidityForAmounts")) {
            ins = _l5("sqrtPriceX96", "sqrtPriceAX96", "sqrtPriceBX96", "amount0", "amount1");
            outKey = "liquidity";
            call = abi.encodeCall(
                this.liqFor,
                (
                    uint160(_u(c, ".in.sqrtPriceX96")),
                    uint160(_u(c, ".in.sqrtPriceAX96")),
                    uint160(_u(c, ".in.sqrtPriceBX96")),
                    _u(c, ".in.amount0"),
                    _u(c, ".in.amount1")
                )
            );
        } else if (_eq(fn, "FullMath.mulDiv") || _eq(fn, "FullMath.mulDivRoundingUp")) {
            ins = _l3("a", "b", "denominator");
            outKey = "result";
            call = abi.encodeCall(
                this.mulDiv,
                (_u(c, ".in.a"), _u(c, ".in.b"), _u(c, ".in.denominator"), _eq(fn, "FullMath.mulDivRoundingUp"))
            );
        } else if (_eq(fn, "LiquidityValue.q")) {
            _keys(c, ".in", _l2("x", "sqrtPriceX96"));
            _keys(c, ".out", _l1("value"));
            assertEq(LiquidityValue.q(_u(c, ".in.x"), uint160(_u(c, ".in.sqrtPriceX96"))), _u(c, ".out.value"), cid);
            return;
        } else {
            assertEq(fn, "LiquidityValue.amounts", cid);
            _keys(c, ".in", _l4("L", "lo", "hi", "sqrtPriceX96"));
            _keys(c, ".out", _l2("amount0", "amount1"));
            (uint256 a0, uint256 a1) = LiquidityValue.amounts(
                uint128(_u(c, ".in.L")), int24(_i(c, ".in.lo")), int24(_i(c, ".in.hi")), uint160(_u(c, ".in.sqrtPriceX96"))
            );
            assertEq(a0, _u(c, ".out.amount0"), string.concat(cid, " amount0"));
            assertEq(a1, _u(c, ".out.amount1"), string.concat(cid, " amount1"));
            return;
        }
        _keys(c, ".in", ins);
        (bool ok, bytes memory ret) = address(this).staticcall(call);
        if (!ok) {
            _keys(c, ".out", _l1("revert"));
            assertEq(_mathErr(ret), _s(c, ".out.revert"), cid);
            return;
        }
        _keys(c, ".out", _l1(outKey));
        string memory o = string.concat(".out.", outKey);
        if (_eq(outKey, "tick")) assertEq(abi.decode(ret, (int256)), _i(c, o), cid);
        else assertEq(abi.decode(ret, (uint256)), _u(c, o), cid);
    }

    function _mathErr(bytes memory ret) internal pure returns (string memory) {
        if (ret.length == 0) return "";
        bytes4 sel = bytes4(ret);
        if (sel == TickMath.InvalidTick.selector) return "InvalidTick";
        if (sel == TickMath.InvalidSqrtPrice.selector) return "InvalidSqrtPrice";
        if (sel == SqrtPriceMath.InvalidPrice.selector) return "InvalidPrice";
        if (sel == SafeCast.SafeCastOverflow.selector) return "SafeCastOverflow";
        return "unknown";
    }

    function test_U_2__ledgerValueVectorsBitForBit() public {
        string[] memory cs = _cases("ledger");
        uint256 n;
        for (uint256 i; i < cs.length; ++i) {
            string memory c = cs[i];
            _keys(c, "$", _l4("id", "fn", "in", "out"));
            cid = _s(c, ".id");
            if (!_eq(_s(c, ".fn"), "ExecLedger.value")) continue;
            _keys(c, ".in", _l2("h", "sqrtP"));
            _keys(c, ".in.h", _l5("idle0", "idle1", "L", "lo", "hi"));
            _keys(c, ".out", _l2("nav", "posValue"));
            Holdings memory h = Holdings(
                uint128(_u(c, ".in.h.idle0")),
                uint128(_u(c, ".in.h.idle1")),
                uint128(_u(c, ".in.h.L")),
                int24(_i(c, ".in.h.lo")),
                int24(_i(c, ".in.h.hi"))
            );
            (uint256 nav, uint256 pv) = ExecLedger.value(h, uint160(_u(c, ".in.sqrtP")));
            assertEq(nav, _u(c, ".out.nav"), string.concat(cid, " nav"));
            assertEq(pv, _u(c, ".out.posValue"), string.concat(cid, " posValue"));
            ++n;
        }
        assertGt(n, 0, "value cases");
    }

    /// @notice An idle vault's NAV is exactly `q(idle0) + idle1` at every price, including the extremes, in both
    ///         token orders at P = 3000 with 18/6 decimals.
    function test_U_2__idleVaultValuationIsExact() public pure {
        // WETH (18) as token0, USDC (6) as token1: P = 3000e6 / 1e18 token1 per token0 unit
        uint160 s1 = 4339505179874779489431521; // floor(sqrt(3000e6/1e18) * 2^96)
        Holdings memory h = Holdings(1e18, 0, 0, 0, 0);
        (uint256 nav,) = ExecLedger.value(h, s1);
        assertEq(nav, LiquidityValue.q(1e18, s1));
        assertApproxEqRel(nav, 3000e6, 1e12, "1 WETH = 3000 USDC");
        // USDC (6) as token0, WETH (18) as token1: P = 1e18 / 3000e6
        uint160 s2 = 1446501726624926496477173928747177; // floor(sqrt(1e18/3000e6) * 2^96)
        h = Holdings(3000e6, 0, 0, 0, 0);
        (nav,) = ExecLedger.value(h, s2);
        assertApproxEqRel(nav, 1e18, 1e12, "3000 USDC = 1 WETH");
        // the extremes do not overflow
        h = Holdings(type(uint128).max, type(uint128).max, 0, 0, 0);
        (nav,) = ExecLedger.value(h, TickMath.MIN_SQRT_PRICE);
        assertEq(nav, uint256(type(uint128).max) + LiquidityValue.q(type(uint128).max, TickMath.MIN_SQRT_PRICE));
        h = Holdings(1, 0, 0, 0, 0);
        (nav,) = ExecLedger.value(h, TickMath.MAX_SQRT_PRICE - 1);
        assertEq(nav, LiquidityValue.q(1, TickMath.MAX_SQRT_PRICE - 1));
    }
}
