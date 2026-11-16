// SPDX-License-Identifier: MIT
pragma solidity 0.8.26;

import {Script} from "forge-std/Script.sol";
import {PoolManager} from "@uniswap/v4-core/src/PoolManager.sol";
import {Hooks} from "@uniswap/v4-core/src/libraries/Hooks.sol";
import {TickMath} from "@uniswap/v4-core/src/libraries/TickMath.sol";
import {Currency} from "@uniswap/v4-core/src/types/Currency.sol";
import {TouchstoneFactory} from "../src/TouchstoneFactory.sol";
import {TouchstoneHook} from "../src/TouchstoneHook.sol";
import {HookArgs} from "../src/interfaces/ITouchstoneFactory.sol";
import {Tier} from "../src/types/Tier.sol";
import {Tiers} from "./Tiers.sol";
import {MockRefPool} from "../test/utils/MockRefPool.sol";
import {MockToken} from "../test/utils/Stubs.sol";

/// @notice Deploys every contract under src/ to a local anvil node, which enforces the EIP-170 code-size limit on
///         a real deployment (`forge test` does not). `script/anvil_check.py` then checks that each one was created
///         and that its deployed runtime is at most 22,000 B.
contract AnvilDeploy is Script {
    address internal constant CREATE2_DEPLOYER = 0x4e59b44847b379578588920cA78FbF26c0B4956C;
    uint160 internal constant FLAGS = Hooks.BEFORE_INITIALIZE_FLAG | Hooks.BEFORE_SWAP_FLAG | Hooks.BEFORE_DONATE_FLAG;

    function run() external {
        vm.startBroadcast();
        PoolManager manager = new PoolManager(msg.sender);
        // TODO(P2/P3): deploy the vault, lens and bundler here and pass the real creation-code hashes.
        new TouchstoneFactory(address(manager), keccak256("vault creation code"), keccak256("hook creation code"));

        // The hook needs a reference with history and an address carrying its permission flags.
        MockToken a = new MockToken("A", 18);
        MockToken b = new MockToken("B", 18);
        (address t0, address t1) = address(a) < address(b) ? (address(a), address(b)) : (address(b), address(a));
        MockRefPool ref = new MockRefPool(t0, t1, 500, uint32(block.timestamp - 2 hours), 0);
        (Tier memory t,) = Tiers.testing();
        t.REF_POOL = address(ref);
        HookArgs memory args = HookArgs({
            poolManager: address(manager),
            vault: address(0xdead),
            currency0: Currency.wrap(t0),
            currency1: Currency.wrap(t1),
            fee: 500,
            tickSpacing: 10,
            seedSqrtPriceX96: TickMath.getSqrtPriceAtTick(0),
            tier: t
        });
        new TouchstoneHook{salt: _mine(args)}(args);
        vm.stopBroadcast();
    }

    function _mine(HookArgs memory args) internal pure returns (bytes32) {
        bytes32 h = keccak256(abi.encodePacked(type(TouchstoneHook).creationCode, abi.encode(args)));
        for (uint256 i;; ++i) {
            address at = address(
                uint160(uint256(keccak256(abi.encodePacked(bytes1(0xff), CREATE2_DEPLOYER, bytes32(i), h))))
            );
            if (uint160(at) & Hooks.ALL_HOOK_MASK == FLAGS) return bytes32(i);
        }
        revert();
    }
}
