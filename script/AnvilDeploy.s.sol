// SPDX-License-Identifier: MIT
pragma solidity 0.8.26;

import {Script} from "forge-std/Script.sol";
import {PoolManager} from "@uniswap/v4-core/src/PoolManager.sol";
import {TickMath} from "@uniswap/v4-core/src/libraries/TickMath.sol";
import {Currency} from "@uniswap/v4-core/src/types/Currency.sol";
import {TouchstoneFactory} from "../src/TouchstoneFactory.sol";
import {TouchstoneHook} from "../src/TouchstoneHook.sol";
import {HookArgs, SeedParams} from "../src/interfaces/ITouchstoneFactory.sol";
import {TouchstoneVault} from "../src/TouchstoneVault.sol";
import {TouchstoneVaultExtension} from "../src/TouchstoneVaultExtension.sol";
import {CuratorBundler, DeployArgs} from "../src/periphery/CuratorBundler.sol";
import {PreSwap} from "../src/types/PreSwap.sol";
import {HookMining} from "./Mine.s.sol";
import {Tier} from "../src/types/Tier.sol";
import {Tiers} from "./Tiers.sol";
import {MockRefPool} from "../test/utils/MockRefPool.sol";
import {MockToken} from "../test/utils/Stubs.sol";

/// @notice Deploys every contract under src/ to a local anvil node, which enforces the EIP-170 code-size limit on
///         a real deployment (`forge test` does not). The vault and the hook are created by the curator's
///         `CuratorBundler.deployAndPlace` through `factory.deploy` with the real creation-code hashes, a hook salt
///         mined for the bundler, the seed, and the first placement in the same transaction. `script/anvil_check.py`
///         then checks that each one was created and that its deployed runtime is at most 22,000 B.
contract AnvilDeploy is Script {
    function run() external {
        vm.startBroadcast();
        address sender = msg.sender;
        PoolManager manager = new PoolManager(sender);
        bytes memory vaultCode = type(TouchstoneVault).creationCode;
        bytes memory extCode = type(TouchstoneVaultExtension).creationCode;
        bytes memory hookCode = type(TouchstoneHook).creationCode;
        TouchstoneFactory factory =
            new TouchstoneFactory(address(manager), keccak256(vaultCode), keccak256(extCode), keccak256(hookCode));

        // The hook needs a reference with history; the factory deploys the vault and the hook and seeds the vault.
        MockToken a = new MockToken("A", 18);
        MockToken b = new MockToken("B", 18);
        (MockToken t0, MockToken t1) = address(a) < address(b) ? (a, b) : (b, a);
        MockRefPool ref = new MockRefPool(address(t0), address(t1), 500, uint32(block.timestamp - 2 hours), 0);
        (Tier memory t,) = Tiers.testing();
        t.REF_POOL = address(ref);
        t.REF_SQRT_PRICE = TickMath.getSqrtPriceAtTick(0);
        bytes32 saltV = bytes32(uint256(1));
        CuratorBundler bundler = new CuratorBundler(sender, factory);
        HookArgs memory args = HookArgs({
            poolManager: address(manager),
            vault: HookMining.vaultAddress(address(factory), address(bundler), saltV, vaultCode),
            currency0: Currency.wrap(address(t0)),
            currency1: Currency.wrap(address(t1)),
            fee: 500,
            tickSpacing: 10,
            seedSqrtPriceX96: TickMath.getSqrtPriceAtTick(0),
            tier: t
        });
        (bytes32 saltH,) = HookMining.mine(address(factory), address(bundler), hookCode, args, 0);
        SeedParams memory seed = SeedParams({a0: 1e18, a1: 1e18, curator: address(bundler), wit0: 1e9, wit1: 1e9});
        t0.mint(address(bundler), 2e18);
        t1.mint(address(bundler), 2e18);
        bundler.deployAndPlace(
            DeployArgs(vaultCode, extCode, hookCode, args, saltV, saltH, seed), -1000, 1000, PreSwap(false, false, 0), type(uint256).max
        );
        vm.stopBroadcast();
    }
}
