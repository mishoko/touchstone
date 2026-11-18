// SPDX-License-Identifier: MIT
pragma solidity 0.8.26;

import {console} from "forge-std/Test.sol";
import {IERC20} from "@openzeppelin/contracts/token/ERC20/IERC20.sol";
import {IPoolManager} from "@uniswap/v4-core/src/interfaces/IPoolManager.sol";
import {StateLibrary} from "@uniswap/v4-core/src/libraries/StateLibrary.sol";
import {TouchstoneVault} from "../../src/TouchstoneVault.sol";
import {IVaultEntries} from "../../src/interfaces/ITouchstoneVaultExtension.sol";
import {MainnetVaultFork, ContractDepositor} from "./MainnetVaultFork.sol";

/// @notice K-1 (spec §9.1) on an Ethereum mainnet fork (chain 1, through `ForkBase._fork`): the factory against the
///         canonical v4 PoolManager, the MAINNET tier against the canonical USDC/WETH 0.05% reference pool, the
///         first placement through `CuratorBundler.deployAndPlace` with real USDC and WETH, then
///         - a quote through the canonical V4Quoter equal to the output of the same swap through the canonical
///           Universal Router (Permit2 approvals, V4_SWAP), both directions;
///         - deposit and withdraw from an EOA and from a contract, never profiting (U-5) and losing at most 1e-6 of what
///           it paid (rounding in the vault's favour).
///         Runs with `FOUNDRY_PROFILE=fork forge test --mp test/fork` (`FORK_BLOCK_1=<n>` pins the block); skips when
///         MAINNET_RPC_URL is unset and fails unless it is chain 1.
contract K1ForkTest is MainnetVaultFork {
    using StateLibrary for IPoolManager;

    function test_K_1__canonicalPoolManagerHostsTheVaultsPool() public {
        if (!_setUp()) return;
        (uint160 sp,,,) = pm.getSlot0(key.toId());
        assertEq(sp, tier.REF_SQRT_PRICE, "the hook pool is initialised at the reference price");
        assertGt(pm.getLiquidity(key.toId()), 0, "the first placement is live in the canonical PoolManager");
        assertEq(TouchstoneVault(payable(vault)).curator(), address(bundler), "the bundler is the curator");
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
