// SPDX-License-Identifier: MIT
pragma solidity 0.8.26;

import {Holdings} from "../types/Holdings.sol";
import {LiquidityValue} from "./LiquidityValue.sol";

/// @title ExecLedger
/// @notice The execution ledger's valuation, a coarse-linked external library. This build carries `value` only; the
///         charge (`measure`) and the markout settlement (`settleExtra`) are added with the curator actions.
library ExecLedger {
    /// @notice NAV of `h` at sqrt price `sqrtP` and the value of its position alone, both in token1 units (floor).
    function value(Holdings calldata h, uint160 sqrtP) external pure returns (uint256 nav, uint256 posValue) {
        return _value(h, sqrtP);
    }

    function _value(Holdings calldata h, uint160 sqrtP) private pure returns (uint256 nav, uint256 posValue) {
        (uint256 x, uint256 y) = LiquidityValue.amounts(h.L, h.lo, h.hi, sqrtP);
        posValue = LiquidityValue.val(x, y, sqrtP);
        nav = LiquidityValue.val(uint256(h.idle0) + x, uint256(h.idle1) + y, sqrtP);
    }
}
