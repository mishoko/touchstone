// SPDX-License-Identifier: MIT
pragma solidity 0.8.26;

import {Script} from "forge-std/Script.sol";
import {PoolManager} from "@uniswap/v4-core/src/PoolManager.sol";
import {TouchstoneFactory} from "../src/TouchstoneFactory.sol";

/// @notice Deploys every contract under src/ to a local anvil node, which enforces the EIP-170 code-size limit on
///         a real deployment (`forge test` does not). `script/anvil_check.py` then checks that each one was created
///         and that its deployed runtime is at most 22,000 B.
contract AnvilDeploy is Script {
    function run() external {
        vm.startBroadcast();
        PoolManager manager = new PoolManager(msg.sender);
        // TODO(P1/P2/P3): deploy the hook, vault, lens and bundler here and pass the real creation-code hashes.
        new TouchstoneFactory(address(manager), keccak256("vault creation code"), keccak256("hook creation code"));
        vm.stopBroadcast();
    }
}
