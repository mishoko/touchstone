// SPDX-License-Identifier: MIT
pragma solidity 0.8.26;

import {PoolKey} from "@uniswap/v4-core/src/types/PoolKey.sol";
import {Hooks} from "@uniswap/v4-core/src/libraries/Hooks.sol";
import {StateLibrary} from "@uniswap/v4-core/src/libraries/StateLibrary.sol";
import {IPoolManager} from "@uniswap/v4-core/src/interfaces/IPoolManager.sol";
import {Currency} from "@uniswap/v4-core/src/types/Currency.sol";
import {TouchstoneFactory} from "../../src/TouchstoneFactory.sol";
import {ITouchstoneFactory, HookArgs, Pending} from "../../src/interfaces/ITouchstoneFactory.sol";
import {HookMining} from "../../script/Mine.s.sol";
import {DeployBaseUnplaced} from "../utils/DeployBaseUnplaced.sol";
import {StubVault, StubVaultBadTier, StubHook} from "../utils/Stubs.sol";

contract FactoryTest is DeployBaseUnplaced {
    using StateLibrary for IPoolManager;

    address internal attacker = makeAddr("attacker");

    function setUp() public override {
        _setUpEnv();
    }

    function test_factory_deploysVaultAndHookAndSeeds() public {
        (vault, hook) = _deploy(deployer, SALT_V);
        StubVault v = StubVault(vault);
        // the vault constructor read the pending handoff
        assertEq(v.HOOK(), hook);
        PoolKey memory k = v.key();
        assertEq(address(k.hooks), hook);
        assertEq(Currency.unwrap(k.currency0), address(token0));
        assertEq(k.fee, FEE);
        assertEq(keccak256(abi.encode(v.tier())), keccak256(abi.encode(tier)));
        assertEq(StubHook(hook).VAULT(), vault);
        // pending is cleared once the vault exists
        Pending memory p = factory.pending();
        assertEq(p.hook, address(0));
        // the hook initialized the pool at the seed price
        (uint160 sqrtP,,,) = IPoolManager(address(manager)).getSlot0(k.toId());
        assertEq(sqrtP, hookArgs.seedSqrtPriceX96);
        // seed: amounts reached the vault (balance-checked) and `seed` was called once
        assertEq(token0.balanceOf(vault), seedParams.a0 + seedParams.wit0);
        assertEq(token1.balanceOf(vault), seedParams.a1 + seedParams.wit1);
        assertEq(v.seedCalls(), 1);
        assertEq(v.curator(), curator);
    }

    function test_factory_minedHookHasExactlyTheFlagBits() public {
        (, hook) = _deploy(deployer, SALT_V);
        assertEq(uint160(hook) & uint160((1 << 14) - 1), HookMining.FLAGS);
        assertTrue(HookMining.hasExactFlags(hook));
    }

    function test_factory_revertsOnVaultCodeHashMismatch() public {
        HookArgs memory a = _hookArgsFor(address(0));
        bytes memory bad = abi.encodePacked(_vaultCode(), hex"00");
        vm.expectRevert(ITouchstoneFactory.CodeHash.selector);
        factory.deploy(bad, _hookCode(), a, SALT_V, bytes32(0), seedParams);
    }

    function test_factory_revertsOnHookCodeHashMismatch() public {
        HookArgs memory a = _hookArgsFor(address(0));
        bytes memory bad = abi.encodePacked(_hookCode(), hex"00");
        vm.expectRevert(ITouchstoneFactory.CodeHash.selector);
        factory.deploy(_vaultCode(), bad, a, SALT_V, bytes32(0), seedParams);
    }

    /// A front-runner copying the deployer's salts and arguments cannot take the deployer's addresses: the salts are
    /// bound to the caller, so the copy is rejected and the deployer's deploy still lands where it was computed.
    function test_factory_saltsBoundToSender() public {
        address victimVault = HookMining.vaultAddress(address(factory), deployer, SALT_V, _vaultCode());
        address attackerVault = HookMining.vaultAddress(address(factory), attacker, SALT_V, _vaultCode());
        assertTrue(victimVault != attackerVault);
        HookArgs memory a = _hookArgsFor(victimVault);
        (bytes32 sH,) = HookMining.mine(address(factory), deployer, _hookCode(), a, 0);
        _fund(attacker);
        vm.prank(attacker);
        vm.expectRevert(ITouchstoneFactory.WrongArgs.selector);
        factory.deploy(_vaultCode(), _hookCode(), a, SALT_V, sH, seedParams);
        // the attacker's own deploy with the same salt values lands elsewhere
        (address av,) = _deploy(attacker, SALT_V);
        assertEq(av, attackerVault);
        // the deployer's deploy is unaffected
        (address v,) = _deploy(deployer, SALT_V);
        assertEq(v, victimVault);
    }

    function test_factory_revertsOnForeignPoolManagerOrVault() public {
        address v = HookMining.vaultAddress(address(factory), deployer, SALT_V, _vaultCode());
        HookArgs memory a = _hookArgsFor(v);
        a.poolManager = address(0xBEEF);
        vm.expectRevert(ITouchstoneFactory.WrongArgs.selector);
        factory.deploy(_vaultCode(), _hookCode(), a, SALT_V, bytes32(0), seedParams);
        a = _hookArgsFor(address(0xBEEF));
        vm.expectRevert(ITouchstoneFactory.WrongArgs.selector);
        factory.deploy(_vaultCode(), _hookCode(), a, SALT_V, bytes32(0), seedParams);
    }

    function test_factory_revertsOnNativeCurrency() public {
        HookArgs memory a = _hookArgsFor(address(0));
        a.currency0 = Currency.wrap(address(0));
        vm.expectRevert(ITouchstoneFactory.BadToken.selector);
        factory.deploy(_vaultCode(), _hookCode(), a, SALT_V, bytes32(0), seedParams);
    }

    function test_factory_revertsWhenVaultTierDiffers() public {
        bytes memory badVault = type(StubVaultBadTier).creationCode;
        factory = new TouchstoneFactory(address(manager), keccak256(badVault), keccak256(_hookCode()));
        address v = HookMining.vaultAddress(address(factory), deployer, SALT_V, badVault);
        HookArgs memory a = _hookArgsFor(v);
        (bytes32 sH,) = HookMining.mine(address(factory), deployer, _hookCode(), a, 0);
        _fund(deployer);
        vm.expectRevert(ITouchstoneFactory.TierMismatch.selector);
        factory.deploy(badVault, _hookCode(), a, SALT_V, sH, seedParams);
    }

    function test_factory_hookAddressWithoutFlagsReverts() public {
        address v = HookMining.vaultAddress(address(factory), deployer, SALT_V, _vaultCode());
        HookArgs memory a = _hookArgsFor(v);
        bytes32 initHash = keccak256(HookMining.hookInitCode(_hookCode(), a));
        uint256 s;
        while (HookMining.hasExactFlags(
                HookMining.create2Address(address(factory), HookMining.boundSalt(deployer, bytes32(s)), initHash)
            )) {
            s++;
        }
        address h = HookMining.create2Address(address(factory), HookMining.boundSalt(deployer, bytes32(s)), initHash);
        _fund(deployer);
        vm.expectRevert(abi.encodeWithSelector(Hooks.HookAddressNotValid.selector, h));
        factory.deploy(_vaultCode(), _hookCode(), a, SALT_V, bytes32(s), seedParams);
    }
}
