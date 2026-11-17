// SPDX-License-Identifier: MIT
pragma solidity 0.8.26;

import {ExecLedger} from "../../src/libraries/ExecLedger.sol";
import {Holdings} from "../../src/types/Holdings.sol";

/// @notice `ExecLedger.settleExtra` reads the pending record through storage pointers (the vault passes its own); a
///         test that uses it as an oracle on in-memory holdings stores them here first.
abstract contract SettleExtraOracle {
    Holdings internal _oracleBefore;
    Holdings internal _oracleAfter;

    function _settleExtra(
        Holdings memory before,
        Holdings memory after_,
        int24 tickMk,
        int24 feeTicks,
        uint256 charge0,
        uint256 reserve,
        Holdings memory now_,
        uint256 S,
        uint256 reserveShares
    ) internal returns (uint256 extra, uint160 pMk, uint256 burnS) {
        (_oracleBefore, _oracleAfter) = (before, after_);
        return ExecLedger.settleExtra(
            _oracleBefore, _oracleAfter, tickMk, feeTicks, charge0, reserve, now_, S, reserveShares
        );
    }
}
