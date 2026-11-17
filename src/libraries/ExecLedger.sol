// SPDX-License-Identifier: MIT
pragma solidity 0.8.26;

import {FullMath} from "@uniswap/v4-core/src/libraries/FullMath.sol";
import {TickMath} from "@uniswap/v4-core/src/libraries/TickMath.sol";
import {Holdings} from "../types/Holdings.sol";
import {LiquidityValue} from "./LiquidityValue.sol";
import {Placement} from "./Placement.sol";
import {Markout} from "../types/Markout.sol";
import {SqrtPriceMath} from "@uniswap/v4-core/src/libraries/SqrtPriceMath.sol";

/// @title ExecLedger
/// @notice The execution ledger, a coarse-linked external library: valuation (`value`), the immediate charge of a
///         curator action with its reserve and solvency gate (`measure`), the markout settlement's extra charge
///         (`settleExtra`), and the placement sizing the vault's actions use (`placementSize`). All values are in
///         token1 units; every function is pure.
library ExecLedger {
    uint256 internal constant WAD = 1e18;
    /// @dev The relative margin on the after holding's fee credit, in pips (spec §3.4).
    uint256 internal constant CREDIT_MARGIN = 1000;

    /// @notice NAV of `h` at sqrt price `sqrtP` and the value of its position alone, both in token1 units (floor).
    function value(Holdings calldata h, uint160 sqrtP) external pure returns (uint256 nav, uint256 posValue) {
        return _value(h, sqrtP);
    }

    /// @notice The immediate charge of an action: the worst NAV loss over the post-move spot, the pre-move spot `s0`
    ///         and the live reference (legs in that order; a later leg replaces only on a strictly larger signed loss,
    ///         so `pStar = sqrtSpot` on a tie and `pStar` is the least gain when every leg is a gain), floored at 0; the
    ///         burn for the charge and for the charge plus the reserve, both rounded up at the one price `navBurn`, the
    ///         smallest `NAV_after` over the three legs (no leg the curator can choose lowers the shares); and the
    ///         solvency gate `burn + burnR ≤ unvested + bond`. A positive amount against `navBurn = 0` saturates.
    function measure(
        Holdings calldata before,
        Holdings calldata after_,
        uint160 sqrtS0,
        uint160 sqrtSpot,
        uint160 sqrtRef,
        uint256 reserve,
        uint256 S,
        uint256 unvested,
        uint256 bond
    ) external pure returns (uint256 charge, uint160 pStar, uint256 navBurn, uint256 burn, uint256 burnR, bool ok) {
        return _measure(before, after_, sqrtS0, sqrtSpot, sqrtRef, reserve, S, unvested, bond);
    }

    /// @notice A curator action's charge in one call (the vault's size lever): the reserve
    ///         `RESERVE_RATIO·NAV_before(s_spot)` (floor), `measure` with that reserve (its gate is applied by
    ///         `FeeBank`'s `Op.Charge` on the same burns), and the epilogue's re-base at the reference on the holdings
    ///         after: `nav = NAV_after(s_ref)` and `delta = min(WAD, positionValue·WAD/nav)` (0 if nav = 0).
    function actionCharge(
        Holdings calldata before,
        Holdings calldata after_,
        uint160 sqrtS0,
        uint160 sqrtSpot,
        uint160 sqrtRef,
        uint256 reserveRatio,
        uint256 S
    )
        external
        pure
        returns (uint256 charge, uint160 pStar, uint256 burn, uint256 burnR, uint256 reserve, uint256 nav, uint64 delta)
    {
        // [MU-195] [MU-196] the reserve on the holdings before the action at the post-move spot
        (uint256 navSpot,) = _value(before, sqrtSpot);
        reserve = FullMath.mulDiv(reserveRatio, navSpot, WAD);
        (charge, pStar,, burn, burnR,) = _measure(before, after_, sqrtS0, sqrtSpot, sqrtRef, reserve, S, 0, 0);
        uint256 pos;
        // [MU-208] the epilogue at the reference
        (nav, pos) = _value(after_, sqrtRef);
        if (nav > 0) {
            uint256 d = FullMath.mulDiv(pos, WAD, nav);
            delta = uint64(d < WAD ? d : WAD);
        }
    }

    /// @notice A swap amount as a fraction of NAV, both valued at the reference on the holdings before the action:
    ///         `ceil(value(amount)·WAD / NAV(sqrtRef))`, the amount in token0 when `zeroForOne`, else in token1;
    ///         `type(uint256).max` when that NAV is 0 (no cap fits).
    function swapFrac(Holdings calldata before, bool zeroForOne, uint256 amount, uint160 sqrtRef)
        external
        pure
        returns (uint256)
    {
        (uint256 nav,) = _value(before, sqrtRef);
        if (nav == 0) return type(uint256).max;
        uint256 v = zeroForOne ? LiquidityValue.val(amount, 0, sqrtRef) : amount;
        return FullMath.mulDivRoundingUp(v, WAD, nav);
    }

    function _measure(
        Holdings calldata before,
        Holdings calldata after_,
        uint160 sqrtS0,
        uint160 sqrtSpot,
        uint160 sqrtRef,
        uint256 reserve,
        uint256 S,
        uint256 unvested,
        uint256 bond
    ) private pure returns (uint256 charge, uint160 pStar, uint256 navBurn, uint256 burn, uint256 burnR, bool ok) {
        // [MU-001] [MU-002] [MU-125] [MU-146] [MU-178] the worse of the three legs; ties keep the spot leg
        (uint256 nb, uint256 na) = _legs(before, after_, sqrtSpot);
        pStar = sqrtSpot;
        // [MU-183] the spot leg is a candidate for the burn price too
        navBurn = na;
        (uint256 nb2, uint256 na2) = _legs(before, after_, sqrtS0);
        if (_worse(nb2, na2, nb, na)) (nb, na, pStar) = (nb2, na2, sqrtS0);
        // [MU-180] the burn price is the smallest NAV_after over the legs
        if (na2 < navBurn) navBurn = na2;
        (nb2, na2) = _legs(before, after_, sqrtRef);
        if (_worse(nb2, na2, nb, na)) (nb, na, pStar) = (nb2, na2, sqrtRef);
        if (na2 < navBurn) navBurn = na2;
        // [MU-003] the 0 floor; [MU-004] the whole loss (100%)
        charge = nb > na ? nb - na : 0;
        // [MU-006] [MU-129] gate and burn at one price, rounded up
        burn = _shares(charge, S, navBurn);
        uint256 tot = _shares(charge + reserve, S, navBurn);
        burnR = tot == type(uint256).max ? type(uint256).max : tot - burn;
        // [MU-147] [MU-177] the reserve's shares are inside the gate; evaluated without overflow
        uint256 cover = unvested > type(uint256).max - bond ? type(uint256).max : unvested + bond;
        ok = burn <= cover && burnR <= cover - burn;
    }

    /// @notice The markout settlement with a successful window read (spec §3.4, the fee-aware settlement): at each leg
    ///         `sqrtAt(max(MIN_TICK, tickMk − feeTicks))`, `sqrtAt(tickMk)`, `sqrtAt(min(MAX_TICK, tickMk + feeTicks + 1))`
    ///         the holdings before (from `s0`, every rounding up) and after (from `sEnd`, every rounding down, less the
    ///         credit margins) are valued with the arbitrage fee their positions earn while spot is carried to the leg;
    ///         the leg with the largest loss fraction `g/nb` is chosen; the burn re-prices the whole charge there:
    ///         `burnS = min(reserveShares, max(0, ceilDiv(loss·sAct, nb) − burn0))`, and `extra` is its value
    ///         `burnS·nb/sAct`. No revert path for valid holdings, prices and a static fee below 10⁶.
    function settleExtra(
        Holdings storage before,
        Holdings storage after_,
        Markout storage pend,
        int24 tickMk,
        int24 feeTicks,
        uint24 feePips,
        uint256 reserveShares
    ) external view returns (uint256 extra, uint160 pMk, uint256 burnS) {
        // `view`: the pending record is read here, through the vault's storage pointers (the vault's size lever)
        Holdings memory b = before;
        // [MU-245]
        Holdings memory a = after_;
        Markout memory pd = pend;
        // the chosen leg: (g > 0, |g|, nb, p)
        bool pos;
        uint256 g;
        uint256 nb;
        for (uint256 i; i < 3; ++i) {
            // [MU-148] [MU-175] [MU-176] [MU-246] lower (FEE_TICKS out, clamped), centre, upper (rounded up, clamped)
            int256 t = i == 0 ? int256(tickMk) - feeTicks : (i == 1 ? int256(tickMk) : int256(tickMk) + feeTicks + 1);
            uint160 p = TickMath.getSqrtPriceAtTick(_clamp(t));
            (uint256 nbi,) = _value(b, p);
            (uint256 nai,) = _value(a, p);
            // [MU-247] the before holding's arbitrage fee, rounded up; [MU-248] the after holding's credit, rounded down
            nbi = _satAdd(nbi, _arbFee(b, pd.s0, p, true, feePips));
            nai = _satAdd(nai, _arbFee(a, pd.sEnd, p, false, feePips));
            bool posi = nbi > nai;
            uint256 gi = posi ? nbi - nai : 0;
            // [MU-249] [MU-261] a positive leg replaces a non-positive one; between positive legs, the larger g/nb
            if (i == 0 || (posi && (!pos || _gt512(gi, nb, g, nbi)))) (pos, g, nb, pMk) = (posi, gi, nbi, p);
        }
        if (!pos || pd.sAct == 0) return (0, pMk, 0);
        // [MU-250] [MU-182] the whole charge re-priced at the leg, rounded up; the quotient is ≤ sAct because g ≤ nb
        uint256 req = FullMath.mulDivRoundingUp(g, pd.sAct, nb);
        // [MU-179] net of the immediate burn
        burnS = req > pd.burn0 ? req - pd.burn0 : 0;
        // [MU-149] capped by the reserve's shares
        if (burnS > reserveShares) burnS = reserveShares;
        // [MU-252] the burn's value
        extra = FullMath.mulDiv(burnS, nb, pd.sAct);
    }

    /// @dev `F(h, s, p, up)` of spec §3.4: the LP fee `h`'s position earns from an exact-input swap that carries spot from
    ///      `s` to `p`, in token1 at `p`. Before (`up`): every rounding up. After (the credit): every rounding down, less
    ///      `(hi − lo) + 2` units of the fee's token before valuation and less `CREDIT_MARGIN` pips after.
    function _arbFee(Holdings memory h, uint160 s, uint160 p, bool up, uint24 feePips) internal pure returns (uint256) {
        if (h.L == 0 || p == s) return 0;
        uint160 x = s < p ? s : p;
        uint160 y = s < p ? p : s;
        uint160 sa = TickMath.getSqrtPriceAtTick(h.lo);
        uint160 sb = TickMath.getSqrtPriceAtTick(h.hi);
        if (x < sa) x = sa;
        // [MU-260]
        if (y > sb) y = sb;
        if (y <= x) return 0;
        // [MU-253] a down move pays token0 in, an up move token1
        bool down = p < s;
        uint256 amt =
            down ? SqrtPriceMath.getAmount0Delta(x, y, h.L, up) : SqrtPriceMath.getAmount1Delta(x, y, h.L, up);
        if (up) {
            // [MU-262] the before holding at the fee plus 2 pips: v4 truncates `protocolFee·lpFee/10⁶` when it composes
            // the swap fee, so with a protocol fee on the LP earns up to ≈ 1.001 pip of rate more per unit of net input
            uint256 fb = feePips + 2;
            uint256 fu = FullMath.mulDivRoundingUp(amt, fb, 1e6 - fb);
            return down ? _qUp(fu, p) : fu;
        }
        uint256 f = FullMath.mulDiv(amt, feePips, 1e6 - feePips);
        // [MU-254] the absolute margin: v4's per-step fee growth floors
        uint256 m = uint256(int256(h.hi) - h.lo) + 2;
        f = f > m ? f - m : 0;
        if (down) f = LiquidityValue.q(f, p);
        // [MU-255] the relative margin
        return FullMath.mulDiv(f, 1e6 - CREDIT_MARGIN, 1e6);
    }

    /// @dev `q` rounded up.
    function _qUp(uint256 x, uint160 s) private pure returns (uint256) {
        if (s <= type(uint128).max) return FullMath.mulDivRoundingUp(uint256(s) * s, x, 1 << 192);
        return FullMath.mulDivRoundingUp(FullMath.mulDivRoundingUp(s, s, 1 << 64), x, 1 << 128);
    }

    function _satAdd(uint256 a, uint256 b) private pure returns (uint256) {
        unchecked {
            uint256 c = a + b;
            return c < a ? type(uint256).max : c;
        }
    }

    /// @dev `a·b > c·d` over 512-bit products. [MU-184]
    function _gt512(uint256 a, uint256 b, uint256 c, uint256 d) private pure returns (bool) {
        uint256 h1 = _mulHi(a, b);
        uint256 h2 = _mulHi(c, d);
        if (h1 != h2) return h1 > h2;
        unchecked {
            return a * b > c * d;
        }
    }

    /// @notice `Placement.size` for the vault's actions (coarse-linked, so the sizing code lives outside the vault).
    function placementSize(Holdings calldata idle, int24 lo, int24 hi, uint160 sqrtSpot, uint160 sqrtRef)
        external
        pure
        returns (uint128 L, Holdings memory after_, uint64 deltaAfter)
    {
        return Placement.size(idle, lo, hi, sqrtSpot, sqrtRef);
    }

    // ------------------------------------------------------------------ internals

    function _value(Holdings memory h, uint160 sqrtP) private pure returns (uint256 nav, uint256 posValue) {
        (uint256 x, uint256 y) = LiquidityValue.amounts(h.L, h.lo, h.hi, sqrtP);
        posValue = LiquidityValue.val(x, y, sqrtP);
        nav = LiquidityValue.val(uint256(h.idle0) + x, uint256(h.idle1) + y, sqrtP);
    }

    function _legs(Holdings memory b, Holdings memory a, uint160 p) private pure returns (uint256 nb, uint256 na) {
        (nb,) = _value(b, p);
        (na,) = _value(a, p);
    }

    /// @dev `nb − na > nbRef − naRef` as signed differences, without casting: `nb + naRef > nbRef + na`.
    function _worse(uint256 nb, uint256 na, uint256 nbRef, uint256 naRef) private pure returns (bool) {
        return nb + naRef > nbRef + na;
    }

    /// @dev `ceilDiv(x·S, nav)`; 0 when x = 0; `type(uint256).max` when x > 0 and nav = 0.
    function _shares(uint256 x, uint256 S, uint256 nav) private pure returns (uint256) {
        if (x == 0) return 0;
        if (nav == 0) return type(uint256).max;
        return FullMath.mulDivRoundingUp(x, S, nav);
    }

    /// @dev The high 256 bits of the 512-bit product `a·b`.
    function _mulHi(uint256 a, uint256 b) private pure returns (uint256 hi) {
        assembly ("memory-safe") {
            let mm := mulmod(a, b, not(0))
            let lo := mul(a, b)
            hi := sub(sub(mm, lo), lt(mm, lo))
        }
    }

    function _clamp(int256 t) private pure returns (int24) {
        if (t < TickMath.MIN_TICK) return TickMath.MIN_TICK;
        if (t > TickMath.MAX_TICK) return TickMath.MAX_TICK;
        return int24(t);
    }
}
