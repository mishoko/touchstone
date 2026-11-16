// SPDX-License-Identifier: MIT
pragma solidity 0.8.26;

import {FullMath} from "@uniswap/v4-core/src/libraries/FullMath.sol";
import {TickMath} from "@uniswap/v4-core/src/libraries/TickMath.sol";
import {Holdings} from "../types/Holdings.sol";
import {LiquidityValue} from "./LiquidityValue.sol";
import {Placement} from "./Placement.sol";

/// @title ExecLedger
/// @notice The execution ledger, a coarse-linked external library: valuation (`value`), the immediate charge of a
///         curator action with its reserve and solvency gate (`measure`), the markout settlement's extra charge
///         (`settleExtra`), and the placement sizing the vault's actions use (`placementSize`). All values are in
///         token1 units; every function is pure.
library ExecLedger {
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
        // [MU-001] [MU-002] [MU-125] [MU-146] [MU-178] the worse of the three legs; ties keep the spot leg
        (uint256 nb, uint256 na) = _legs(before, after_, sqrtSpot);
        pStar = sqrtSpot;
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

    /// @notice The markout settlement's extra charge with a successful window read: the worse of the legs
    ///         `sqrtAt(max(MIN_TICK, tickMk − feeTicks))` and `sqrtAt(min(MAX_TICK, tickMk + feeTicks + 1))` (the upper
    ///         leg rounded up, since `tickMk` is the floor of the window's mean; the lower leg on a tie), net of the
    ///         immediate charge and capped at the reserve; the reserve shares to burn, rounded up at `NAV(now, pMk)`
    ///         and capped at `reserveShares` (all of them if that NAV is 0 and extra > 0). No revert path on the legs.
    function settleExtra(
        Holdings calldata before,
        Holdings calldata after_,
        int24 tickMk,
        int24 feeTicks,
        uint256 charge0,
        uint256 reserve,
        Holdings calldata now_,
        uint256 S,
        uint256 reserveShares
    ) external pure returns (uint256 extra, uint160 pMk, uint256 burnS) {
        // [MU-148] the legs sit FEE_TICKS outside the markout tick
        // [MU-175] the upper leg is rounded up; [MU-176] both legs are clamped to the tick range
        pMk = TickMath.getSqrtPriceAtTick(_clamp(int256(tickMk) - feeTicks));
        uint160 pHi = TickMath.getSqrtPriceAtTick(_clamp(int256(tickMk) + feeTicks + 1));
        (uint256 nb, uint256 na) = _legs(before, after_, pMk);
        (uint256 nb2, uint256 na2) = _legs(before, after_, pHi);
        if (_worse(nb2, na2, nb, na)) (nb, na, pMk) = (nb2, na2, pHi);
        uint256 loss = nb > na ? nb - na : 0;
        // [MU-149] [MU-179] net of the immediate charge, capped at the reserve
        extra = loss > charge0 ? loss - charge0 : 0;
        if (extra > reserve) extra = reserve;
        if (extra == 0) return (0, pMk, 0);
        (uint256 navNow,) = _value(now_, pMk);
        if (navNow == 0) return (extra, pMk, reserveShares);
        // overflow-free cap: ceilDiv(extra·S, navNow) > reserveShares ⇔ extra > floor(reserveShares·navNow/S). When that
        // floor exceeds 2^256 the cap cannot bind, and then extra·S/navNow < 1 for uint128 extra and reserveShares (the
        // two quotients multiply to extra·reserveShares < 2^256), so the rounded-up division below never overflows.
        if (S == 0) return (extra, pMk, 0);
        // [MU-182] the cap's division is taken only when it fits
        if (_mulHi(reserveShares, navNow) < S && extra > FullMath.mulDiv(reserveShares, navNow, S)) {
            return (extra, pMk, reserveShares);
        }
        burnS = FullMath.mulDivRoundingUp(extra, S, navNow);
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

    function _value(Holdings calldata h, uint160 sqrtP) private pure returns (uint256 nav, uint256 posValue) {
        (uint256 x, uint256 y) = LiquidityValue.amounts(h.L, h.lo, h.hi, sqrtP);
        posValue = LiquidityValue.val(x, y, sqrtP);
        nav = LiquidityValue.val(uint256(h.idle0) + x, uint256(h.idle1) + y, sqrtP);
    }

    function _legs(Holdings calldata b, Holdings calldata a, uint160 p) private pure returns (uint256 nb, uint256 na) {
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
