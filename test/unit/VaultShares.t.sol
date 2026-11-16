// SPDX-License-Identifier: MIT
pragma solidity 0.8.26;

import {VaultBase, ClaimDonor} from "../utils/VaultBase.sol";
import {TouchstoneVault} from "../../src/TouchstoneVault.sol";
import {Holdings} from "../../src/types/Holdings.sol";
import {TickMath} from "@uniswap/v4-core/src/libraries/TickMath.sol";

/// @notice ShareMath, seed and the NAV caps on the real vault (EMPTY range, TEST tier).
contract VaultSharesTest is VaultBase {
    // ------------------------------------------------------------------ U-5: ShareMath

    function _ceilDiv(uint256 a, uint256 b) internal pure returns (uint256) {
        return a == 0 ? 0 : (a - 1) / b + 1;
    }

    function test_U_5__depositChargesEveryBucketRoundedUp() public {
        _fundUser(alice, 10e18);
        uint256 S = v.totalSupply();
        uint256 s = 333_333_333_333_333_333_333; // idle·s/S is not an integer
        Holdings memory h = v.holdings();
        (uint256 a0, uint256 a1) = _deposit(alice, s);
        assertEq(a0, _ceilDiv(uint256(h.idle0) * s, S), "token0 rounded up");
        assertEq(a1, _ceilDiv(uint256(h.idle1) * s, S), "token1 rounded up");
        assertGt(a0 * S, uint256(h.idle0) * s, "strictly above the exact share");
        assertEq(v.balanceOf(alice), s);
        Holdings memory h2 = v.holdings();
        assertEq(h2.idle0, h.idle0 + a0, "idle0 tracks the payment");
        assertEq(h2.idle1, h.idle1 + a1, "idle1 tracks the payment");
    }

    function test_U_5__oneShareOfANonZeroBucketCostsAtLeastOneWei() public {
        _fundUser(alice, 1e18);
        (uint256 a0, uint256 a1) = _deposit(alice, 1);
        assertEq(a0, 1);
        assertEq(a1, 1);
    }

    function test_U_5__withdrawPaysRoundedDownOnThePreBurnSupply() public {
        _fundUser(alice, 10e18);
        _deposit(alice, 7e23);
        Holdings memory h = v.holdings();
        uint256 S = v.totalSupply();
        uint256 s = 333_333_333_333_333_333_333;
        (uint256 o0, uint256 o1) = _withdraw(alice, s);
        assertEq(o0, uint256(h.idle0) * s / S, "token0 rounded down, pre-burn supply");
        assertEq(o1, uint256(h.idle1) * s / S, "token1 rounded down, pre-burn supply");
        assertLt(o0 * S, uint256(h.idle0) * s, "strictly below the exact share");
        assertEq(token0.balanceOf(alice), 10e18 - _ceilDiv(1e18 * 7e23, 1e24) + o0);
    }

    function test_U_5__zeroSharesRevert() public {
        _fundUser(alice, 1e18);
        vm.prank(alice);
        vm.expectRevert(TouchstoneVault.ZeroShares.selector);
        v.deposit(0, type(uint256).max, type(uint256).max, alice);
        vm.prank(alice);
        vm.expectRevert(TouchstoneVault.ZeroShares.selector);
        v.withdraw(0, 0, 0, alice);
    }

    function test_U_5__slippageBoundsBothWays() public {
        _fundUser(alice, 10e18);
        vm.prank(alice);
        vm.expectRevert(TouchstoneVault.Slippage.selector);
        v.deposit(1e24, 1e18 - 1, type(uint256).max, alice); // costs exactly 1e18 of token0
        _deposit(alice, 1e24);
        vm.prank(alice);
        vm.expectRevert(TouchstoneVault.Slippage.selector);
        v.withdraw(1e24, 1e18 + 1, 0, alice); // pays exactly 1e18 of token0 (S = 2e24, idle0 = 2e18)
    }

    /// @notice A deposit followed by a withdrawal of the same shares never returns more than it paid.
    function test_U_5__roundTripNeverProfits(uint256 s, uint256 t) public {
        s = bound(s, 1, 5e25);
        t = bound(t, 1, 5e25);
        _fundUser(alice, 1e24);
        _fundUser(bob, 1e24);
        _deposit(bob, t); // a third party changes the ratios first
        uint256 b0 = token0.balanceOf(alice);
        uint256 b1 = token1.balanceOf(alice);
        _deposit(alice, s);
        _withdraw(alice, s);
        assertLe(token0.balanceOf(alice), b0, "token0");
        assertLe(token1.balanceOf(alice), b1, "token1");
    }

    // ------------------------------------------------------------------ U-navcap

    function test_U_navcap__bondCapInSharesAtTheBoundary() public {
        _fundUser(alice, 1e30);
        uint256 cap = v.bondShares() * 1e18 / tier.BOND_RATIO; // the largest S with S·R <= bond·WAD
        uint256 room = cap - v.totalSupply();
        vm.prank(alice);
        vm.expectRevert(TouchstoneVault.NavCap.selector);
        v.deposit(room + 1, type(uint256).max, type(uint256).max, alice);
        _deposit(alice, room);
        assertEq(v.totalSupply(), cap);
        assertLe(v.totalSupply() * tier.BOND_RATIO, v.bondShares() * 1e18);
    }

    function test_U_navcap__seedNav0ExcludesTheWitnessAndBankLimitsAreWritten() public view {
        // seed 1e18 + 1e18 at tick 0 (price 1): NAV0 = 2e18 exactly; the 1e9 + 1e9 witness funding is not counted
        assertEq(v.nav0(), 2e18, "NAV0 at seed");
        (uint40 drip, uint40 rel) = v.bankLimits();
        assertEq(drip, tier.FORFEIT_DRIP_BLOCKS, "dripBlocks");
        assertEq(rel, 4 * tier.EPOCH, "releaseDelay = V * EPOCH");
        assertEq(v.bondShares(), 1e24 - 1e15, "bond = seed shares minus DEAD");
        assertEq(v.balanceOf(address(0xdEaD)), 1e15, "DEAD");
        assertEq(v.balanceOf(address(v)), v.bondShares(), "bond held by the vault");
        assertEq(v.curator(), curator, "curator bound at seed");
    }
}

/// @notice The tier cap NAV_MAX, BOND_MIN and the seed checks need their own deployments.
contract VaultSeedTest is VaultBase {
    function setUp() public override {
        _setUpEnv();
    }

    function _deployWith() internal {
        (vault, hook) = _deploy(deployer, SALT_V);
        v = TouchstoneVault(vault);
    }

    function test_U_navcap__navMaxOnTheRebasedNav0() public {
        // NAV0 = 2e18 at seed and NAV0' = floor(2e18·S'/1e24): one wei of NAV0 is 5e5 shares
        tier.NAV_MAX = 3e18;
        _deployWith();
        _fundUser(alice, 10e18);
        vm.prank(alice);
        vm.expectRevert(TouchstoneVault.NavCap.selector);
        v.deposit(5e23 + 5e5, type(uint256).max, type(uint256).max, alice); // NAV0' = 3e18 + 1
        _deposit(alice, 5e23 + 5e5 - 1); // NAV0' = 3e18
        assertEq(v.nav0(), 3e18, "rebased NAV0 at the cap");
    }

    function test_U_navcap__navMaxControlWithoutTheCap() public {
        _deployWith(); // TEST tier: NAV_MAX = 2^128 - 1
        _fundUser(alice, 10e18);
        _deposit(alice, 5e23 + 5e5);
        assertEq(v.nav0(), 3e18 + 1, "the over-cap deposit is admitted without the cap");
    }

    function test_U_navcap__bondMinAtTheReferencePriceNotTheSeedPrice() public {
        // the reference price is at tick -10,000 (p ≈ 0.368): bond value ≈ 1.368e18 there, 2e18 at the seed price
        tier.REF_SQRT_PRICE = TickMath.getSqrtPriceAtTick(-10_000);
        tier.BOND_MIN = 1.5e18;
        _expectSeedRevert(TouchstoneVault.BondMin.selector);
    }

    function test_U_navcap__bondMinPassesJustBelowTheBondValue() public {
        tier.REF_SQRT_PRICE = TickMath.getSqrtPriceAtTick(-10_000);
        tier.BOND_MIN = 1.3e18;
        _deployWith();
        assertEq(v.totalSupply(), 1e24);
    }

    function test_U_navcap__valuelessSeedReverts() public {
        seedParams.a0 = 0;
        seedParams.a1 = 0;
        _expectSeedRevert(TouchstoneVault.ZeroShares.selector);
    }

    function _expectSeedRevert(bytes4 sel) internal {
        vm.expectRevert(sel);
        this.deployExt();
    }

    function deployExt() external {
        (vault, hook) = _deploy(deployer, SALT_V);
    }
}
