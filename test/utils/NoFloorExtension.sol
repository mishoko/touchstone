// SPDX-License-Identifier: MIT
pragma solidity 0.8.26;

import {TouchstoneVaultExtension} from "../../src/TouchstoneVaultExtension.sol";

/// @notice Test-only: the extension with the poke's witness gas floor removed (the seam `_witnessGasMin`), for the
///         positive control of the gas-starved poke (F-048 (b), s19 fix review S19F-01). It never ships.
contract NoFloorExtension is TouchstoneVaultExtension {
    function _witnessGasMin() internal pure override returns (uint256) {
        return 0;
    }
}
