// SPDX-License-Identifier: MIT
pragma solidity 0.8.26;

import {Script} from "forge-std/Script.sol";
import {Hooks} from "@uniswap/v4-core/src/libraries/Hooks.sol";
import {HookArgs} from "../src/interfaces/ITouchstoneFactory.sol";

/// @notice Off-chain address computation for `TouchstoneFactory.deploy`. Salts are bound to the caller of `deploy`
///         (`keccak256(abi.encode(sender, salt))`), so every address here is computed for a given `sender`.
library HookMining {
    /// @notice The hook's permission bits: beforeInitialize, beforeSwap and beforeDonate, and nothing else.
    uint160 internal constant FLAGS = Hooks.BEFORE_INITIALIZE_FLAG | Hooks.BEFORE_SWAP_FLAG | Hooks.BEFORE_DONATE_FLAG;
    uint256 internal constant MAX_ITERATIONS = 1_000_000;

    function boundSalt(address sender, bytes32 salt) internal pure returns (bytes32) {
        return keccak256(abi.encode(sender, salt));
    }

    function create2Address(address deployer, bytes32 salt, bytes32 initCodeHash) internal pure returns (address) {
        return address(uint160(uint256(keccak256(abi.encodePacked(bytes1(0xff), deployer, salt, initCodeHash)))));
    }

    /// @notice The vault's address; its initcode has no constructor arguments.
    function vaultAddress(address factory, address sender, bytes32 saltV, bytes memory vaultCode)
        internal
        pure
        returns (address)
    {
        return create2Address(factory, boundSalt(sender, saltV), keccak256(vaultCode));
    }

    function hookInitCode(bytes memory hookCode, HookArgs memory args) internal pure returns (bytes memory) {
        return abi.encodePacked(hookCode, abi.encode(args));
    }

    function hasExactFlags(address hook) internal pure returns (bool) {
        return uint160(hook) & Hooks.ALL_HOOK_MASK == FLAGS;
    }

    /// @notice Finds the first `saltH >= start` whose hook address carries exactly `FLAGS` and has no code yet.
    function mine(address factory, address sender, bytes memory hookCode, HookArgs memory args, uint256 start)
        internal
        view
        returns (bytes32 saltH, address hook)
    {
        bytes32 initHash = keccak256(hookInitCode(hookCode, args));
        for (uint256 i = start; i < start + MAX_ITERATIONS; i++) {
            hook = create2Address(factory, boundSalt(sender, bytes32(i)), initHash);
            if (hasExactFlags(hook) && hook.code.length == 0) return (bytes32(i), hook);
        }
        revert("HookMining: no salt found");
    }
}

/// @notice Mining entry point for deploy scripts: mine `saltH` over the same `HookArgs` the deploy transaction uses,
///         in the same script run (the seed price is part of the hash).
contract Mine is Script {
    function mineHookSalt(address factory, address sender, bytes memory hookCode, HookArgs memory args)
        public
        view
        returns (bytes32 saltH, address hook)
    {
        return HookMining.mine(factory, sender, hookCode, args, 0);
    }
}
