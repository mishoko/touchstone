// SPDX-License-Identifier: MIT
pragma solidity 0.8.26;

import {console} from "forge-std/Test.sol";
import {IERC20} from "@openzeppelin/contracts/token/ERC20/IERC20.sol";
import {IHooks} from "@uniswap/v4-core/src/interfaces/IHooks.sol";
import {IPoolManager} from "@uniswap/v4-core/src/interfaces/IPoolManager.sol";
import {PoolKey} from "@uniswap/v4-core/src/types/PoolKey.sol";
import {PoolId} from "@uniswap/v4-core/src/types/PoolId.sol";
import {Currency} from "@uniswap/v4-core/src/types/Currency.sol";
import {StateLibrary} from "@uniswap/v4-core/src/libraries/StateLibrary.sol";
import {TickMath} from "@uniswap/v4-core/src/libraries/TickMath.sol";
import {SqrtPriceMath} from "@uniswap/v4-core/src/libraries/SqrtPriceMath.sol";
import {LiquidityAmounts} from "@uniswap/v4-periphery/src/libraries/LiquidityAmounts.sol";
import {IV4Router} from "@uniswap/v4-periphery/src/interfaces/IV4Router.sol";
import {IV4Quoter} from "@uniswap/v4-periphery/src/interfaces/IV4Quoter.sol";
import {Actions} from "@uniswap/v4-periphery/src/libraries/Actions.sol";
import {TouchstoneFactory} from "../../src/TouchstoneFactory.sol";
import {TouchstoneHook} from "../../src/TouchstoneHook.sol";
import {TouchstoneVault} from "../../src/TouchstoneVault.sol";
import {TouchstoneVaultExtension} from "../../src/TouchstoneVaultExtension.sol";
import {CuratorBundler, DeployArgs} from "../../src/periphery/CuratorBundler.sol";
import {HookArgs, SeedParams} from "../../src/interfaces/ITouchstoneFactory.sol";
import {IUniswapV3PoolOracle} from "../../src/interfaces/IUniswapV3PoolOracle.sol";
import {IVaultEntries} from "../../src/interfaces/ITouchstoneVaultExtension.sol";
import {PreSwap} from "../../src/types/PreSwap.sol";
import {Tier} from "../../src/types/Tier.sol";
import {HookMining} from "../../script/Mine.s.sol";
import {Tiers} from "../../script/Tiers.sol";
import {Addresses} from "../../script/Addresses.sol";
import {ForkBase} from "./ForkBase.sol";

interface IUniversalRouter {
    function execute(bytes calldata commands, bytes[] calldata inputs, uint256 deadline) external payable;
}

interface IPermit2 {
    function approve(address token, address spender, uint160 amount, uint48 expiration) external;
}

/// @notice A contract depositor: holds shares and calls the vault itself (K-1: "deposit/withdraw from a contract").
contract ContractDepositor {
    function approve(IERC20 t, address spender) external {
        t.approve(spender, type(uint256).max);
    }

    function deposit(address vault, uint256 shares) external returns (uint256, uint256) {
        return IVaultEntries(vault).deposit(shares, type(uint256).max, type(uint256).max, address(this));
    }

    function withdraw(address vault, uint256 shares) external returns (uint256, uint256) {
        return IVaultEntries(vault).withdraw(shares, 0, 0, address(this));
    }
}

/// @notice K-1 (spec §9.1) on an Ethereum mainnet fork (chain 1, through `ForkBase._fork`): the factory against the
///         canonical v4 PoolManager, the MAINNET tier against the canonical USDC/WETH 0.05% reference pool, the
///         first placement through `CuratorBundler.deployAndPlace` with real USDC and WETH, then
///         - a quote through the canonical V4Quoter equal to the output of the same swap through the canonical
///           Universal Router (Permit2 approvals, V4_SWAP), both directions;
///         - deposit and withdraw from an EOA and from a contract, never profiting (U-5) and losing at most 1e-6 of what
///           it paid (rounding in the vault's favour).
///         Runs with `FOUNDRY_PROFILE=fork forge test --mp test/fork` (`FORK_BLOCK_1=<n>` pins the block); skips when
///         MAINNET_RPC_URL is unset and fails unless it is chain 1.
contract K1ForkTest is ForkBase {
    using StateLibrary for IPoolManager;

    uint24 internal constant FEE = 500;
    int24 internal constant SPACING = 10;
    uint8 internal constant V4_SWAP = 0x10;

    IPoolManager internal pm = IPoolManager(Addresses.MAINNET_POOL_MANAGER);
    IUniswapV3PoolOracle internal ref = IUniswapV3PoolOracle(Addresses.MAINNET_REF_POOL_USDC_WETH_500);
    IERC20 internal usdc;
    IERC20 internal weth;
    Tier internal tier;
    address internal curator = makeAddr("curator");
    address internal alice = makeAddr("alice");
    CuratorBundler internal bundler;
    address internal vault;
    address internal hook;
    PoolKey internal key;

    function _twapTick(uint32 window) internal view returns (int24 tick) {
        uint32[] memory ago = new uint32[](2);
        ago[0] = window;
        (int56[] memory c,) = ref.observe(ago);
        int56 d = c[1] - c[0];
        tick = int24(d / int56(uint56(window)));
        if (d < 0 && d % int56(uint56(window)) != 0) --tick;
    }

    function _setUp() internal returns (bool) {
        if (!_fork("MAINNET_RPC_URL", 1)) return false;
        usdc = IERC20(ref.token0());
        weth = IERC20(ref.token1());
        (Tier memory t, uint256 blocktime) = Tiers.mainnet();
        int24 refTick = _twapTick(t.REF_WINDOW);
        uint160 seedSqrt = TickMath.getSqrtPriceAtTick(refTick);
        t.REF_SQRT_PRICE = seedSqrt;
        Tiers.check(t, blocktime);
        tier = t;

        bytes memory vaultCode = type(TouchstoneVault).creationCode;
        bytes memory extCode = type(TouchstoneVaultExtension).creationCode;
        bytes memory hookCode = type(TouchstoneHook).creationCode;
        TouchstoneFactory factory =
            new TouchstoneFactory(address(pm), keccak256(vaultCode), keccak256(extCode), keccak256(hookCode));
        bundler = new CuratorBundler(curator, factory);
        bytes32 saltV = bytes32(uint256(1));
        HookArgs memory args = HookArgs({
            poolManager: address(pm),
            vault: HookMining.vaultAddress(address(factory), address(bundler), saltV, vaultCode),
            currency0: Currency.wrap(address(usdc)),
            currency1: Currency.wrap(address(weth)),
            fee: FEE,
            tickSpacing: SPACING,
            seedSqrtPriceX96: seedSqrt,
            tier: t
        });
        (bytes32 saltH,) = HookMining.mine(address(factory), address(bundler), hookCode, args, 0);

        int24 c = refTick / SPACING * SPACING;
        if (refTick < 0 && refTick % SPACING != 0) c -= SPACING;
        int24 lo = c - t.W_TICKS / 2;
        int24 hi = c + t.W_TICKS / 2;
        uint128 l = LiquidityAmounts.getLiquidityForAmount1(TickMath.getSqrtPriceAtTick(lo), seedSqrt, 2e18);
        uint256 a0 = SqrtPriceMath.getAmount0Delta(seedSqrt, TickMath.getSqrtPriceAtTick(hi), l, true);
        uint256 a1 = SqrtPriceMath.getAmount1Delta(TickMath.getSqrtPriceAtTick(lo), seedSqrt, l, true);
        SeedParams memory seed =
            SeedParams({a0: a0 * 101 / 100 + 1, a1: a1 * 101 / 100 + 1, curator: address(bundler), wit0: 1e6, wit1: 1e12});
        deal(address(usdc), address(bundler), seed.a0 + seed.wit0);
        deal(address(weth), address(bundler), seed.a1 + seed.wit1);
        vm.prank(curator);
        (vault, hook) = bundler.deployAndPlace(
            DeployArgs(vaultCode, extCode, hookCode, args, saltV, saltH, seed), lo, hi, PreSwap(false, false, 0), type(uint256).max
        );
        key = PoolKey(Currency.wrap(address(usdc)), Currency.wrap(address(weth)), FEE, SPACING, IHooks(hook));
        // the first placement's markout settles after its window; deposits wait for it (MarkoutPending)
        vm.roll(vm.getBlockNumber() + 2 + tier.H_MARKOUT / blocktime);
        vm.warp(vm.getBlockTimestamp() + (2 + tier.H_MARKOUT / blocktime) * blocktime);
        vm.prank(curator);
        bundler.settle();
        return true;
    }

    function test_K_1__canonicalPoolManagerHostsTheVaultsPool() public {
        if (!_setUp()) return;
        (uint160 sp,,,) = pm.getSlot0(key.toId());
        assertEq(sp, tier.REF_SQRT_PRICE, "the hook pool is initialised at the reference price");
        assertGt(pm.getLiquidity(key.toId()), 0, "the first placement is live in the canonical PoolManager");
        assertEq(TouchstoneVault(payable(vault)).curator(), address(bundler), "the bundler is the curator");
    }

    function _swapViaRouter(address who, bool zeroForOne, uint128 amountIn) internal returns (uint256 out) {
        IERC20 tin = zeroForOne ? usdc : weth;
        IERC20 tout = zeroForOne ? weth : usdc;
        deal(address(tin), who, amountIn);
        vm.startPrank(who);
        tin.approve(Addresses.MAINNET_PERMIT2, type(uint256).max);
        IPermit2(Addresses.MAINNET_PERMIT2).approve(
            address(tin), Addresses.MAINNET_UNIVERSAL_ROUTER, type(uint160).max, type(uint48).max
        );
        bytes memory actions = abi.encodePacked(uint8(Actions.SWAP_EXACT_IN_SINGLE), uint8(Actions.SETTLE_ALL), uint8(Actions.TAKE_ALL));
        bytes[] memory params = new bytes[](3);
        params[0] = abi.encode(IV4Router.ExactInputSingleParams(key, zeroForOne, amountIn, 0, ""));
        params[1] = abi.encode(zeroForOne ? key.currency0 : key.currency1, uint256(amountIn));
        params[2] = abi.encode(zeroForOne ? key.currency1 : key.currency0, uint256(0));
        bytes[] memory inputs = new bytes[](1);
        inputs[0] = abi.encode(actions, params);
        uint256 before = tout.balanceOf(who);
        IUniversalRouter(Addresses.MAINNET_UNIVERSAL_ROUTER).execute(
            abi.encodePacked(V4_SWAP), inputs, vm.getBlockTimestamp() + 60
        );
        vm.stopPrank();
        assertEq(tin.balanceOf(who), 0, "the router took the exact input");
        out = tout.balanceOf(who) - before;
    }

    function _quote(bool zeroForOne, uint128 amountIn) internal returns (uint256 q) {
        (q,) = IV4Quoter(Addresses.MAINNET_V4_QUOTER).quoteExactInputSingle(
            IV4Quoter.QuoteExactSingleParams(key, zeroForOne, amountIn, "")
        );
    }

    /// @notice The canonical V4Quoter quotes exactly what the canonical Universal Router then pays, both directions,
    ///         through the hook's `beforeSwap` (a new block each time, so the hook snapshots).
    function test_K_1__quoterMatchesTheUniversalRouterSwap() public {
        if (!_setUp()) return;
        address trader = makeAddr("trader");
        uint128 inUsdc = 100e6;
        uint256 q = _quote(true, inUsdc);
        uint256 out = _swapViaRouter(trader, true, inUsdc);
        console.log("K-1 USDC->WETH 100e6: quote, out", q, out);
        assertGt(out, 0, "a USDC->WETH swap pays out");
        assertEq(out, q, "quote = swap (USDC->WETH)");
        vm.roll(vm.getBlockNumber() + 1);
        vm.warp(vm.getBlockTimestamp() + 12);
        uint128 inWeth = 0.05e18;
        q = _quote(false, inWeth);
        out = _swapViaRouter(trader, false, inWeth);
        console.log("K-1 WETH->USDC 0.05e18: quote, out", q, out);
        assertGt(out, 0, "a WETH->USDC swap pays out");
        assertEq(out, q, "quote = swap (WETH->USDC)");
    }

    /// @dev U-5's property on real tokens (a round trip never profits), and a loss of at most 1e-6 of what was paid
    ///      plus 2 wei per token (rounding in the vault's favour; logged by the callers).
    function _roundTripAssert(uint256 p0, uint256 p1, uint256 r0, uint256 r1) internal pure {
        assertLe(r0, p0, "no more USDC out than in");
        assertLe(r1, p1, "no more WETH out than in");
        assertGe(r0 + p0 / 1e6 + 2, p0, "USDC back within 1e-6");
        assertGe(r1 + p1 / 1e6 + 2, p1, "WETH back within 1e-6");
    }

    ///         what it paid, less at most rounding in the vault's favour: it never gets back more
    ///         than it paid (U-5) and loses at most 1e-6 of it (measured: one liquidity unit, ~1,168 wei of WETH).
    function test_K_1__depositAndWithdrawFromAnEOA() public {
        if (!_setUp()) return;
        deal(address(usdc), alice, 1e12);
        deal(address(weth), alice, 1e21);
        vm.startPrank(alice);
        usdc.approve(vault, type(uint256).max);
        weth.approve(vault, type(uint256).max);
        uint256 shares = IERC20(vault).totalSupply() / 4; // a quarter of the vault: a real-size deposit
        (uint256 p0, uint256 p1) = IVaultEntries(vault).deposit(shares, type(uint256).max, type(uint256).max, alice);
        assertEq(IERC20(vault).balanceOf(alice), shares, "shares minted to the EOA");
        (uint256 r0, uint256 r1) = IVaultEntries(vault).withdraw(shares, 0, 0, alice);
        vm.stopPrank();
        console.log("K-1 EOA paid, got back (USDC)", p0, r0);
        console.log("K-1 EOA paid, got back (WETH)", p1, r1);
        assertGt(p0 + p1, 0, "the deposit paid tokens");
        _roundTripAssert(p0, p1, r0, r1);
        assertEq(IERC20(vault).balanceOf(alice), 0, "shares burned");
    }

    /// @notice The same round trip from a contract that holds the shares and calls the vault itself.
    function test_K_1__depositAndWithdrawFromAContract() public {
        if (!_setUp()) return;
        ContractDepositor d = new ContractDepositor();
        deal(address(usdc), address(d), 1e12);
        deal(address(weth), address(d), 1e21);
        d.approve(usdc, vault);
        d.approve(weth, vault);
        uint256 shares = IERC20(vault).totalSupply() / 4;
        (uint256 p0, uint256 p1) = d.deposit(vault, shares);
        console.log("K-1 contract paid (USDC, WETH)", p0, p1);
        assertEq(IERC20(vault).balanceOf(address(d)), shares, "shares minted to the contract");
        (uint256 r0, uint256 r1) = d.withdraw(vault, shares);
        console.log("K-1 contract got back (USDC, WETH)", r0, r1);
        assertGt(p0 + p1, 0, "the deposit paid tokens");
        _roundTripAssert(p0, p1, r0, r1);
        assertEq(usdc.balanceOf(address(d)), 1e12 - p0 + r0, "USDC accounted");
        assertEq(weth.balanceOf(address(d)), 1e21 - p1 + r1, "WETH accounted");
    }
}
