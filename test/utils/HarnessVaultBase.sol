// SPDX-License-Identifier: MIT
pragma solidity 0.8.26;

import {VaultBase} from "./VaultBase.sol";
import {VaultHarness} from "./VaultHarness.sol";
import {ITouchstoneHook} from "../../src/interfaces/ITouchstoneHook.sol";
import {Obs} from "../../src/types/Obs.sol";

/// @notice The real vault flow through the factory, with `VaultHarness` as the vault code (EMPTY range, TEST tier).
abstract contract HarnessVaultBase is VaultBase {
    VaultHarness internal hv;

    function _vaultCode() internal pure virtual override returns (bytes memory) {
        return type(VaultHarness).creationCode;
    }

    function setUp() public virtual override {
        super.setUp();
        hv = VaultHarness(vault);
    }

    function _obs() internal view returns (Obs memory) {
        return ITouchstoneHook(hook).obs();
    }

    /// @notice Rolls `blocks` in steps of 10 with a keep-alive refresh after each, so every span is observed.
    function _keepAlive(uint256 blocks) internal {
        for (uint256 i; i < blocks; i += 10) {
            advance(10);
            ITouchstoneHook(hook).refreshRef();
        }
    }

    /// @notice Keeps the clock observed until `n` more breaches have latched (EMPTY or out of range).
    function _driveBreaches(uint32 n) internal {
        uint32 target = _obs().breachSeq + n;
        for (uint256 i; _obs().breachSeq < target; ++i) {
            require(i < 1000, "no breach");
            _keepAlive(10);
        }
    }
}
