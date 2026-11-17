// SPDX-License-Identifier: MIT
pragma solidity 0.8.26;

import {console} from "forge-std/console.sol";
import {TickMath} from "@uniswap/v4-core/src/libraries/TickMath.sol";
import {FullMath} from "@uniswap/v4-core/src/libraries/FullMath.sol";
import {VaultBase} from "../utils/VaultBase.sol";
import {VaultView} from "../utils/VaultView.sol";
import {TouchstoneVault} from "../../src/TouchstoneVault.sol";
import {ITouchstoneHook} from "../../src/interfaces/ITouchstoneHook.sol";
import {SeedParams} from "../../src/interfaces/ITouchstoneFactory.sol";
import {ExecLedger} from "../../src/libraries/ExecLedger.sol";

using VaultView for TouchstoneVault;

/// @notice U-navcap, the stale-`NAV₀` rows (owner-items panel D3, F-037; disclosed, no on-chain check): deposits are
///         capped on the price-free re-based `NAV₀`, which is priced at the last action (here the seed), so after a
///         price move the vault's own NAV at the reference can pass `NAV_MAX`. `NAV_MAX` sits at a quarter of the deep
///         reference pool's depth (`L_deep`/4), so the overshoot must stay inside that 4× haircut. Three rows on an
///         unplaced vault holding half its NAV in token0 (the seed), `NAV_MAX` = 100 token1 (below the bond cap, so it
///         binds): the +25% example (token0 up 50%), the ≈ +50% worst case (token0 doubles: ETH halved in a USDC/ETH
///         pool), and pure drift (the vault at the cap, then token0 doubles, no deposit).
abstract contract NavCapRows is VaultBase {
    uint128 internal constant CAP = 100e18;
    /// @dev The depth haircut: `NAV_MAX` = `L_deep`/4.
    uint256 internal constant HAIRCUT = 4e18;

    function setUp() public override {
        _setUpEnv();
        tier.NAV_MAX = CAP;
        (vault, hook) = _deploy(deployer, SALT_V);
        v = TouchstoneVault(vault);
        _fundUser(alice, 1e30);
    }

    /// @dev The reference (the market) moves to `t`; 51 blocks pass with keep-alive refreshes so the TWAP reads `t`.
    function _move(int24 t) internal {
        refPool.setTick(t);
        for (uint256 i; i < 51; i += 17) {
            advance(17);
            ITouchstoneHook(hook).refreshRef();
        }
        assertEq(ITouchstoneHook(hook).liveRef(), t, "the reference reads the market");
    }

    /// @dev Deposits the largest share amount the cap admits (`floor(NAV₀·S'/S) ≤ NAV_MAX`), after checking that the
    ///      smallest amount past it reverts `NavCap` (a share is worth less than a wei of NAV here).
    function _fillToNavCap() internal {
        uint256 S = v.totalSupply();
        uint256 over = FullMath.mulDivRoundingUp(uint256(CAP) + 1, S, v.nav0()) - S;
        vm.prank(alice);
        vm.expectRevert(TouchstoneVault.NavCap.selector);
        v.deposit(over, type(uint256).max, type(uint256).max, alice);
        _deposit(alice, over - 1);
        assertEq(v.nav0(), CAP, "NAV0 at the cap");
    }

    /// @dev The vault's own NAV at the reference over `NAV_MAX` (WAD).
    function _overshoot() internal view returns (uint256) {
        (uint256 nav,) = ExecLedger.value(v.holdings(), TickMath.getSqrtPriceAtTick(ITouchstoneHook(hook).liveRef()));
        return FullMath.mulDiv(nav, 1e18, CAP);
    }

}

/// @notice The two-sided rows (the seed's half-token0 vault).
contract VaultNavCapTest is NavCapRows {
    /// @notice The +25% example: token0 up 50% (≈ 4,055 ticks) since `NAV₀` was set; deposits fill the cap on the stale
    ///         `NAV₀`; the vault's NAV at the reference is ≈ 1.25 × `NAV_MAX`, inside the 4× haircut.
    function test_U_navcap__staleNav0OvershootTwentyFivePercentExample() public {
        _move(4055);
        _fillToNavCap();
        uint256 o = _overshoot();
        console.log("U-navcap +25% example: NAV(s_ref) / NAV_MAX (1e18 = 1)", o);
        assertApproxEqRel(o, 1.25e18, 0.01e18, "about +25%");
        assertLt(o, HAIRCUT, "inside the 4x depth haircut");
    }

    /// @notice Half the NAV in token0 and token0 doubles (ETH halved against USDC): ≈ +50%. Not the worst case: a
    ///         one-sided vault reaches ×2 at the same doubling (below; fix review S14-07).
    function test_U_navcap__staleNav0OvershootHalfToken0DoublingFiftyPercent() public {
        _move(6932);
        _fillToNavCap();
        uint256 o = _overshoot();
        console.log("U-navcap half token0, doubling: NAV(s_ref) / NAV_MAX (1e18 = 1)", o);
        assertApproxEqRel(o, 1.5e18, 0.01e18, "about +50%");
        assertLt(o, HAIRCUT, "inside the 4x depth haircut");
    }

    /// @notice Pure drift: the vault filled to the cap at the seed price, then token0 doubles with no deposit.
    function test_U_navcap__pureDriftWithNoDepositStaysInsideTheHaircut() public {
        _fillToNavCap();
        assertApproxEqRel(_overshoot(), 1e18, 0.001e18, "at the cap at the seed price");
        _move(6932);
        uint256 o = _overshoot();
        console.log("U-navcap pure drift: NAV(s_ref) / NAV_MAX (1e18 = 1)", o);
        assertApproxEqRel(o, 1.5e18, 0.01e18, "about +50% with no deposit");
        assertLt(o, HAIRCUT, "inside the 4x depth haircut");
    }
}

/// @notice The one-sided rows (fix review S14-07): a vault holding (almost) only token0, as an exit state can. The
///         overshoot is the price move itself: ×2 at a doubling, and the 4× haircut is reached at token0 ×4 (its
///         break-even move, ≈ 13,863 ticks), which a one-sided vault crosses only after a fourfold move with no action.
contract VaultNavCapOneSidedTest is NavCapRows {
    function _seedParams() internal view virtual override returns (SeedParams memory p) {
        p = super._seedParams();
        p.a1 = p.a1 / 1e6; // ~all of the NAV in token0
    }

    function test_U_navcap__oneSidedDoublingIsTwofold() public {
        _fillToNavCap();
        _move(6932);
        uint256 o = _overshoot();
        console.log("U-navcap one-sided, doubling: NAV(s_ref) / NAV_MAX (1e18 = 1)", o);
        assertApproxEqRel(o, 2e18, 0.01e18, "about x2");
        assertLt(o, HAIRCUT, "inside the 4x depth haircut");
    }

    function test_U_navcap__oneSidedBreakEvenAtFourfold() public {
        _fillToNavCap();
        _move(13863);
        uint256 o = _overshoot();
        console.log("U-navcap one-sided, token0 x4: NAV(s_ref) / NAV_MAX (1e18 = 1)", o);
        assertApproxEqRel(o, HAIRCUT, 0.01e18, "the haircut's break-even: token0 x4");
    }
}

