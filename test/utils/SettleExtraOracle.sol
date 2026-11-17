// SPDX-License-Identifier: MIT
pragma solidity 0.8.26;

import {ExecLedger} from "../../src/libraries/ExecLedger.sol";
import {Holdings} from "../../src/types/Holdings.sol";
import {Markout} from "../../src/types/Markout.sol";

/// @notice `ExecLedger.settleExtra` reads the pending record through storage pointers (the vault passes its own); a
///         test that uses it as an oracle on in-memory values stores them here first.
abstract contract SettleExtraOracle {
    Holdings internal _oracleBefore;
    Holdings internal _oracleAfter;
    Markout internal _oracleMarkout;

    function _settleExtra(
        Holdings memory before,
        Holdings memory after_,
        Markout memory m,
        int24 tickMk,
        int24 feeTicks,
        uint24 feePips,
        uint256 reserveShares
    ) internal returns (uint256 extra, uint160 pMk, uint256 burnS) {
        (_oracleBefore, _oracleAfter, _oracleMarkout) = (before, after_, m);
        return ExecLedger.settleExtra(_oracleBefore, _oracleAfter, _oracleMarkout, tickMk, feeTicks, feePips, reserveShares);
    }
}
