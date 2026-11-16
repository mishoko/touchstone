// SPDX-License-Identifier: MIT
pragma solidity 0.8.26;

import {Hooks} from "@uniswap/v4-core/src/libraries/Hooks.sol";

/// @notice CREATE2 with the constructor's revert data bubbled up, and a salt miner for the hook's flag bits.
contract Create2Deployer {
    uint160 internal constant FLAGS = Hooks.BEFORE_INITIALIZE_FLAG | Hooks.BEFORE_SWAP_FLAG | Hooks.BEFORE_DONATE_FLAG;

    function deploy(bytes memory init, bytes32 salt) external returns (address a) {
        assembly ("memory-safe") {
            a := create2(0, add(init, 0x20), mload(init), salt)
            if iszero(a) {
                let p := mload(0x40)
                returndatacopy(p, 0, returndatasize())
                revert(p, returndatasize())
            }
        }
    }

    function mine(bytes memory init) external view returns (bytes32 salt, address at) {
        bytes32 hash = keccak256(init);
        for (uint256 i;; ++i) {
            at = address(uint160(uint256(keccak256(abi.encodePacked(bytes1(0xff), address(this), bytes32(i), hash)))));
            if (uint160(at) & Hooks.ALL_HOOK_MASK == FLAGS && at.code.length == 0) return (bytes32(i), at);
        }
    }
}
