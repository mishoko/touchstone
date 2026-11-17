// SPDX-License-Identifier: MIT
pragma solidity 0.8.26;

import {FullMath} from "@uniswap/v4-core/src/libraries/FullMath.sol";
import {TickMath} from "@uniswap/v4-core/src/libraries/TickMath.sol";
import {ExecLedger} from "../../src/libraries/ExecLedger.sol";
import {Holdings} from "../../src/types/Holdings.sol";

/// @notice The s14 settlement arithmetic (the form before the fee-aware repair of F-044), kept only as the positive
///         control's seam: legs `tickMk − feeTicks` and `tickMk + feeTicks + 1` on the holdings alone (no arbitrage
///         fee, no centre leg); `extra = min(reserve, max(0, loss − charge0))`, the immediate charge netted at face
///         value; `burnS = min(reserveShares, ceilDiv(extra·S, NAV_now(pMk)))` at today's holdings and supply.
library SettleS14 {
    function settleExtra(
        Holdings memory before,
        Holdings memory after_,
        int24 tickMk,
        int24 feeTicks,
        uint256 charge0,
        uint256 reserve,
        Holdings memory now_,
        uint256 S,
        uint256 reserveShares
    ) internal pure returns (uint256 extra, uint256 burnS) {
        uint160 pLo = TickMath.getSqrtPriceAtTick(_clamp(int256(tickMk) - feeTicks));
        uint160 pHi = TickMath.getSqrtPriceAtTick(_clamp(int256(tickMk) + feeTicks + 1));
        (uint256 bl,) = ExecLedger.value(before, pLo);
        (uint256 al,) = ExecLedger.value(after_, pLo);
        (uint256 bh,) = ExecLedger.value(before, pHi);
        (uint256 ah,) = ExecLedger.value(after_, pHi);
        (uint256 nb, uint256 na, uint160 pMk) = bh + al > bl + ah ? (bh, ah, pHi) : (bl, al, pLo);
        uint256 loss = nb > na ? nb - na : 0;
        extra = loss > charge0 ? loss - charge0 : 0;
        if (extra > reserve) extra = reserve;
        if (extra == 0 || S == 0) return (extra, 0);
        (uint256 navNow,) = ExecLedger.value(now_, pMk);
        if (navNow == 0) return (extra, reserveShares);
        burnS = FullMath.mulDivRoundingUp(extra, S, navNow);
        if (burnS > reserveShares) burnS = reserveShares;
    }

    function _clamp(int256 t) private pure returns (int24) {
        if (t < TickMath.MIN_TICK) return TickMath.MIN_TICK;
        if (t > TickMath.MAX_TICK) return TickMath.MAX_TICK;
        return int24(t);
    }
}
