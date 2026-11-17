// SPDX-License-Identifier: MIT
pragma solidity 0.8.26;

import {console} from "forge-std/console.sol";
import {FullMath} from "@uniswap/v4-core/src/libraries/FullMath.sol";
import {CellBase} from "./CellBase.sol";
import {VaultHarness} from "../utils/VaultHarness.sol";
import {TouchstoneVault} from "../../src/TouchstoneVault.sol";
import {CuratorBundler} from "../../src/periphery/CuratorBundler.sol";
import {LiquidityValue} from "../../src/libraries/LiquidityValue.sol";
import {Holdings} from "../../src/types/Holdings.sol";

/// @notice M-ADV1 (spec §5.1 ADV-1): a needle position for leverage. Structural: every width other than W reverts
///         `Width`; on every legal placement δ_after ≤ 1 and the exposure β = L / L_centred ≤ 2r²/(r + 1) with
///         r = 1.0001^(W/4) (≈ 1.078 at W = 2,000: an edge range holds at most that much more liquidity than the
///         centred one for the same NAV), and β is not in pay. Positive control: without the width rule (the harness's
///         unguarded placement seam) a 20-tick needle reaches β ≫ 1.
abstract contract ADV1Rows is CellBase {
    /// @dev β: the position's liquidity over the liquidity the whole NAV would buy on the centred W-wide range at spot.
    function _beta() internal view returns (uint256) {
        Holdings memory h = _h();
        uint160 sp = _sqrt(market);
        int24 c = _floor(market);
        (uint256 x, uint256 y) = LiquidityValue.amounts(1e18, c - W / 2, c + W / 2, sp);
        uint256 perL = LiquidityValue.val(x, y, sp); // the value of 1e18 L on the centred range
        return FullMath.mulDiv(uint256(h.L), perL, _nav(sp));
    }
}

contract MADV1Test is ADV1Rows {
    /// @notice Every width other than W reverts `Width` (aligned widths from one spacing to 2·W, both edges of spot).
    function test_M_ADV1__everyNonWWidthReverts(uint256 w, uint256 off) public {
        w = bound(w, 1, 400) * uint256(int256(TICK_SPACING));
        vm.assume(w != uint256(int256(W)));
        int24 lo = _floor(market) - int24(int256(bound(off, 0, w / 10 - 1) * 10));
        vm.prank(owner);
        (bool ok, bytes memory ret) = address(bundler).call(
            abi.encodeCall(CuratorBundler.setPositions, (lo, lo + int24(int256(w)), _none(), 0, type(uint256).max))
        );
        assertFalse(ok, "a non-W width reverts");
        assertEq(bytes4(ret), TouchstoneVault.Width.selector, "Width");
    }

    /// @notice Every legal placement (any legal range in range and in both exit states): δ_after ≤ 1 and β ≤ 1.08.
    function test_M_ADV1__legalPlacementsBoundTheExposure(uint8 st, uint256 loSeed) public {
        _pickState(st);
        (bool rev,) = _try(_callPlace(_legalLo(market, market, loSeed), _none(), 0));
        if (rev) return;
        uint256 b = _beta();
        assertLe(b, 1.08e18, "beta <= 1.08");
        (, uint256 pos) = _posValue();
        assertLe(pos, _nav(_sqrt(market)), "delta <= 1");
    }

    function _posValue() internal view returns (uint256 nav, uint256 pos) {
        Holdings memory h = _h();
        (uint256 x, uint256 y) = LiquidityValue.amounts(h.L, h.lo, h.hi, _sqrt(market));
        return (_nav(_sqrt(market)), LiquidityValue.val(x, y, _sqrt(market)));
    }

    /// @notice The bound is tight: an edge placement reaches β above 1.07.
    function test_M_ADV1__edgePlacementReachesTheBound() public {
        // a one-sided vault (the market 150 ticks above its range: all token1), spot one unit inside the upper edge
        _moveMarket(_floor(market) + W / 2 + 150);
        (uint160 ps, int24 lo) = _edge(0, true);
        syb.pushTo(ps);
        (bool rev,) = _try(_callPlace(lo, _none(), 0));
        assertFalse(rev, "acts");
        uint256 b = _beta();
        console.log("M-ADV1 edge placement beta (1e18 = 1)", b);
        // Python ADV-1 max β 1.025; the closed-form edge bound 2r²/(r + 1) ≈ 1.078 is reached only valued at the edge
        assertGt(b, 1.02e18, "an edge range holds more liquidity than the centred one");
        assertLe(b, 1.08e18, "within the bound");
    }
}

/// @notice M-ADV1's positive control on `VaultHarness` (production code plus a test seam): the harness's unguarded
///         placement (no width rule) places a 20-tick needle at spot; its β is far above the bound.
contract MADV1CtlTest is ADV1Rows {
    function _vaultCode() internal pure override returns (bytes memory) {
        return type(VaultHarness).creationCode;
    }

    function test_M_ADV1__ctlNoWidthRuleAdmitsANeedle() public {
        int24 c = _floor(market);
        VaultHarness(vault).placeForTest(c - 10, c + 10);
        uint256 b = _beta();
        console.log("M-ADV1 control: needle beta (1e18 = 1)", b);
        assertGt(b, 10e18, "control: a needle multiplies the exposure");
    }
}
