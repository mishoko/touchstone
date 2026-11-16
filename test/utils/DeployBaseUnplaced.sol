// SPDX-License-Identifier: MIT
pragma solidity 0.8.26;

import {Test} from "forge-std/Test.sol";
import {PoolManager} from "@uniswap/v4-core/src/PoolManager.sol";
import {Currency} from "@uniswap/v4-core/src/types/Currency.sol";
import {TickMath} from "@uniswap/v4-core/src/libraries/TickMath.sol";
import {TouchstoneFactory} from "../../src/TouchstoneFactory.sol";
import {HookArgs, SeedParams} from "../../src/interfaces/ITouchstoneFactory.sol";
import {Tier} from "../../src/types/Tier.sol";
import {Tiers} from "../../script/Tiers.sol";
import {HookMining} from "../../script/Mine.s.sol";
import {MockRefPool} from "./MockRefPool.sol";
import {StubVault, MockToken} from "./Stubs.sol";
import {TouchstoneHook} from "../../src/TouchstoneHook.sol";

/// @notice Test base: a local PoolManager, two mock tokens, a scripted reference pool, the factory, and one vault and
///         hook deployed through `factory.deploy` at the TEST tier with no range placed (the EMPTY variant).
///         Time starts at `ts >= 1e6` and `block >= GRID`; `advance(n)` rolls n blocks and warps n * blocktime.
abstract contract DeployBaseUnplaced is Test {
    int24 internal constant SEED_TICK = 0;
    uint24 internal constant FEE = 500;
    int24 internal constant TICK_SPACING = 10;
    bytes32 internal constant SALT_V = bytes32(uint256(1));

    PoolManager internal manager;
    MockToken internal token0;
    MockToken internal token1;
    MockRefPool internal refPool;
    TouchstoneFactory internal factory;
    Tier internal tier;
    uint256 internal blocktime;
    address internal deployer; // the `deploy` caller: salt sender and seed payer
    address internal curator = makeAddr("curator");
    SeedParams internal seedParams;

    address internal vault;
    address internal hook;
    HookArgs internal hookArgs;
    bytes32 internal saltH;

    // The factory and hook tests use a stub vault; `VaultBase` overrides this with the real vault.
    function _vaultCode() internal pure virtual returns (bytes memory) {
        return type(StubVault).creationCode;
    }

    /// @dev Test tokens; a suite may substitute a token with transfer callbacks.
    function _newToken(string memory n, uint8 d) internal virtual returns (MockToken) {
        return new MockToken(n, d);
    }

    function _hookCode() internal pure virtual returns (bytes memory) {
        return type(TouchstoneHook).creationCode;
    }

    function setUp() public virtual {
        _setUpEnv();
        (vault, hook) = _deploy(deployer, SALT_V);
    }

    function _setUpEnv() internal {
        (Tier memory t, uint256 bt) = Tiers.testing();
        blocktime = bt;
        vm.warp(1_000_000);
        vm.roll(t.GRID);
        manager = new PoolManager(address(this));
        MockToken a = _newToken("WETH", 18);
        MockToken b = _newToken("USDC", 6);
        (token0, token1) = address(a) < address(b) ? (a, b) : (b, a);
        refPool = new MockRefPool(address(token0), address(token1), FEE, uint32(block.timestamp - 2 hours), SEED_TICK);
        t.REF_POOL = address(refPool);
        t.REF_SQRT_PRICE = TickMath.getSqrtPriceAtTick(SEED_TICK);
        Tiers.check(t, bt);
        tier = t;
        factory = new TouchstoneFactory(address(manager), keccak256(_vaultCode()), keccak256(_hookCode()));
        deployer = address(this);
        seedParams = SeedParams({a0: 1e18, a1: 1e18, curator: curator, wit0: 1e9, wit1: 1e9});
    }

    function _hookArgsFor(address vaultAddr) internal view returns (HookArgs memory) {
        return HookArgs({
            poolManager: address(manager),
            vault: vaultAddr,
            currency0: Currency.wrap(address(token0)),
            currency1: Currency.wrap(address(token1)),
            fee: FEE,
            tickSpacing: TICK_SPACING,
            seedSqrtPriceX96: TickMath.getSqrtPriceAtTick(SEED_TICK),
            tier: tier
        });
    }

    function _fund(address payer) internal {
        uint256 n0 = seedParams.a0 + seedParams.wit0;
        uint256 n1 = seedParams.a1 + seedParams.wit1;
        token0.mint(payer, n0);
        token1.mint(payer, n1);
        vm.startPrank(payer);
        token0.approve(address(factory), n0);
        token1.approve(address(factory), n1);
        vm.stopPrank();
    }

    /// @notice Computes the vault address, mines the hook salt for `sender`, funds `sender` and deploys.
    function _deploy(address sender, bytes32 saltV) internal returns (address v, address h) {
        address predictedVault = HookMining.vaultAddress(address(factory), sender, saltV, _vaultCode());
        hookArgs = _hookArgsFor(predictedVault);
        address predictedHook;
        (saltH, predictedHook) = HookMining.mine(address(factory), sender, _hookCode(), hookArgs, 0);
        _fund(sender);
        vm.prank(sender);
        (v, h) = factory.deploy(_vaultCode(), _hookCode(), hookArgs, saltV, saltH, seedParams);
        assertEq(v, predictedVault, "vault address");
        assertEq(h, predictedHook, "hook address");
    }

    function advance(uint256 n) internal {
        vm.roll(block.number + n);
        vm.warp(block.timestamp + n * blocktime);
    }
}
