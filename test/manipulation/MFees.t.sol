// SPDX-License-Identifier: MIT
pragma solidity 0.8.26;

import {console} from "forge-std/console.sol";
import {StateLibrary} from "@uniswap/v4-core/src/libraries/StateLibrary.sol";
import {FullMath} from "@uniswap/v4-core/src/libraries/FullMath.sol";
import {CellBase} from "./CellBase.sol";
import {VaultHarness} from "../utils/VaultHarness.sol";
import {TouchstoneVault} from "../../src/TouchstoneVault.sol";
import {VaultView} from "../utils/VaultView.sol";
import {LiquidityValue} from "../../src/libraries/LiquidityValue.sol";
import {Holdings} from "../../src/types/Holdings.sol";
import {PreSwap} from "../../src/types/PreSwap.sol";

using VaultView for TouchstoneVault;

/// @notice The fee cells' world: in range with idle (an honest placement with spot 30 ticks below the upper edge leaves
///         token0 idle), and helpers for the wash and the vault's uncollected fees.
abstract contract FeeRows is CellBase {
    using StateLibrary for *;

    function setUp() public virtual override {
        super.setUp();
        _honestPlace(_floor(market) + 30 - W);
    }

    /// @dev `n` round trips of ±`x` ticks around the market, then back to the market, by `who` (the Sybil or, through
    ///      `_thirdPartyPush`, a third party).
    function _wash(int24 x, uint256 n, bool sybil) internal {
        for (uint256 i; i < n; ++i) {
            if (sybil) {
                syb.pushTo(_sqrt(market + x));
                syb.pushTo(_sqrt(market - x));
            } else {
                _thirdPartyPush(_sqrt(market + x));
                _thirdPartyPush(_sqrt(market - x));
            }
        }
        if (sybil) syb.pushTo(_sqrt(market));
        else _thirdPartyPush(_sqrt(market));
    }

    /// @dev The vault's NAV at the market including the fees its position has accrued but not collected.
    function _navWithFees() internal view returns (uint256) {
        Holdings memory h = _h();
        uint256 nav = _nav(_sqrt(market));
        if (h.L == 0) return nav;
        (, uint256 last0, uint256 last1) = manager.getPositionInfo(poolId, vault, h.lo, h.hi, bytes32(0));
        (uint256 g0, uint256 g1) = manager.getFeeGrowthInside(poolId, h.lo, h.hi);
        uint256 f0;
        uint256 f1;
        unchecked {
            f0 = FullMath.mulDiv(g0 - last0, h.L, 1 << 128);
            f1 = FullMath.mulDiv(g1 - last1, h.L, 1 << 128);
        }
        return nav + LiquidityValue.val(f0, f1, _sqrt(market));
    }
}

/// @notice M-3 (spec §5.1 T3): curator wash trading. The Sybil round-trips volume through the vault's range so the
///         vault earns fees the coalition pays; then a checkpoint, the curator's `rebalanceSwap` of 2% of NAV, or an
///         honest re-placement. Pay never depends on fees, so the coalition loses, and the swap's charge is the same
///         with and without the wash (collect-first sweeps the wash fees into idle before NAV_before).
contract M3Test is FeeRows {
    int24[3] internal XS = [int24(5), 20, 60];
    uint256[3] internal NS = [uint256(2), 10, 40];

    function _followUp(uint256 kind) internal returns (bool reverted) {
        if (kind == 0) {
            v.settle();
            return false;
        }
        if (kind == 1) {
            (reverted,) = _try(_callSwap(true, _amt0(2e16)));
            return reverted;
        }
        (reverted,) = _try(_callPlace(_floor(market) + 30 - W, _none(), 0));
    }

    function _amt0(uint256 frac) internal view returns (uint128) {
        uint256 v1 = FullMath.mulDiv(_nav(_sqrt(market)), frac, WAD);
        uint160 sp = _sqrt(market);
        return uint128(FullMath.mulDiv(FullMath.mulDiv(v1, 1 << 96, sp), 1 << 96, sp));
    }

    /// @notice The grid: (x, n) ∈ {(5, 2), (20, 10), (60, 40)} × third-party depth 0 and 1 × the three follow-ups.
    function test_M_3__washTradingLoses() public {
        for (uint256 w; w < 3; ++w) {
            for (uint256 k; k < 2; ++k) {
                for (uint256 kind; kind < 3; ++kind) {
                    uint256 snap = vm.snapshotState();
                    _thirdPartyDepth(k * 1e18);
                    v.settle();
                    int256 s0 = _sybilValue();
                    uint256 c0 = _curatorShares();
                    int256 w0 = _wealth();
                    _wash(XS[w], NS[w], true);
                    bool rev = _followUp(kind);
                    assertFalse(rev, "the follow-up acts");
                    _settleDue();
                    Out memory o;
                    _close(o, w0, s0, c0);
                    _assertLoses(o, "M-3");
                    vm.revertToState(snap);
                }
            }
        }
    }

    /// @notice The swap's charge with the wash equals the charge without it (to 1 wei), for each of the three washes
    ///         at third-party depth 0 and 1 (a loop: the domain has six values).
    function test_M_3__rebalanceSwapChargeUnchangedByTheWash() public {
        for (uint256 i; i < 6; ++i) {
            uint256 snap0 = vm.snapshotState();
            if (i >= 3) _thirdPartyDepth(1e18);
            uint256 snap = vm.snapshotState();
            (bool r1,) = _try(_callSwap(true, _amt0(2e16)));
            (uint256 cNo,) = _pendingCharge();
            vm.revertToState(snap);
            _wash(XS[i % 3], NS[i % 3], true);
            (bool r2,) = _try(_callSwap(true, _amt0(2e16)));
            (uint256 cWash,) = _pendingCharge();
            assertFalse(r1 || r2, "both swaps act");
            assertGt(cNo, 0, "charged");
            assertApproxEqAbs(cWash, cNo, 1, "the charge does not depend on the wash");
            vm.revertToState(snap0);
        }
    }
}

/// @notice M-3's positive control on `VaultHarness` (production code plus a test seam; the vault's runtime is
///         unchanged): a fee-linked pay, s = 1 (the fees collected first are paid to the curator as new shares while
///         its bond keeps its share of them). The wash then profits.
contract M3CtlTest is FeeRows {
    function _vaultCode() internal pure override returns (bytes memory) {
        return type(VaultHarness).creationCode;
    }

    function test_M_3__ctlFeeLinkedPayProfits() public {
        VaultHarness(vault).setControls(false, address(bundler), false, false);
        v.settle();
        int256 s0 = _sybilValue();
        uint256 c0 = _curatorShares();
        int256 w0 = _wealth();
        _wash(60, 40, true);
        v.settle();
        Out memory o;
        _close(o, w0, s0, c0);
        assertGt(o.net, 0, "control: the wash profits under a fee-linked pay");
        console.log("M-3 control net (wei)", o.net);
    }
}

/// @notice M-5b (spec §5.1, §9.1): a re-ratio over a fee-rich segment. A third party's volume accrues fees in the
///         vault's position; the curator then re-places (with or without a pre-swap). Collect-first sweeps the fees
///         into idle before NAV_before, so the charge covers the action's realised loss at the market (fees on both
///         sides).
contract M5bTest is FeeRows {
    /// @dev Third-party depth 0.3× the vault's L, so the re-ratio's pre-swap fills once the vault's own position is
    ///      removed (fix review S14-02: without it the pre-swap filled nothing).
    function setUp() public virtual override {
        super.setUp();
        _thirdPartyDepth(0.3e18);
    }

    /// @notice Fuzz: the fee-rich segment's size, the new range, the pre-swap's size (up to 2% of NAV) and side.
    function test_M_5b__chargeCoversTheRealisedLossOverAFeeRichSegment(uint8 n, uint256 loSeed, uint256 frac, bool z)
        public
    {
        _wash(40, 1 + uint256(n) % 40, false);
        frac = bound(frac, 0, 2e16);
        PreSwap memory ps = frac == 0 ? _none() : PreSwap(true, z, _amount(z, frac));
        (bool rev, uint256 charge, uint256 realised, uint256 swapped) = _reRatio(_legalLo(market, market, loSeed), ps);
        if (rev) return;
        if (frac > 0) assertGt(swapped, 0, "non-vacuity: the pre-swap fills");
        assertGe(charge + 1, realised, "M-5b: the charge covers the realised loss");
    }

    /// @notice The pinned row: 40 round trips of ±40 ticks, then an in-place re-ratio with a 2% pre-swap.
    function test_M_5b__pinnedFeeRichReRatio() public {
        _wash(40, 40, false);
        assertGt(_navWithFees(), _nav(_sqrt(market)), "fees accrued");
        (bool rev, uint256 charge, uint256 realised, uint256 swapped) =
            _reRatio(_floor(market) + 30 - W, PreSwap(true, true, _amount(true, 2e16)));
        assertFalse(rev, "acts");
        assertGt(swapped, 0, "the pre-swap fills");
        assertGt(realised, 0, "a realised loss");
        assertGe(charge + 1, realised, "M-5b: the charge covers the realised loss");
        console.log("M-5b: charge, realised loss", charge, realised);
    }

    function _amount(bool z, uint256 frac) internal view returns (uint128) {
        uint256 v1 = FullMath.mulDiv(_nav(_sqrt(market)), frac, WAD);
        if (!z) return uint128(v1);
        uint160 sp = _sqrt(market);
        return uint128(FullMath.mulDiv(FullMath.mulDiv(v1, 1 << 96, sp), 1 << 96, sp));
    }

    /// @dev The re-placement on `[lo, lo + W)`; returns its charge (immediate plus the markout's extra) and the realised
    ///      loss: NAV at the market with the uncollected fees before, minus NAV at the market after.
    function _reRatio(int24 lo, PreSwap memory ps)
        internal
        returns (bool rev, uint256 charge, uint256 realised, uint256 swapped)
    {
        uint256 navB = _navWithFees();
        (rev,, swapped) = _tryRec(_callPlace(lo, ps, 0));
        if (rev) return (true, 0, 0, 0);
        (charge,) = _pendingCharge();
        uint256 navA = _navWithFees();
        realised = navB > navA ? navB - navA : 0;
        charge += _settleDueExtra();
    }
}

/// @notice M-5b's positive control on `VaultHarness`: no collect-first (the action's own removal pays the accrued
///         fees into idle inside the measured action), so the fees offset the loss and the charge falls short.
contract M5bCtlTest is FeeRows {
    function _vaultCode() internal pure override returns (bytes memory) {
        return type(VaultHarness).creationCode;
    }

    function test_M_5b__ctlNoCollectFirstUndercharges() public {
        _thirdPartyDepth(0.3e18);
        _wash(40, 40, false);
        VaultHarness(vault).setControls(true, address(0), false, false);
        uint256 navB = _navWithFees();
        uint256 v1 = FullMath.mulDiv(_nav(_sqrt(market)), 2e16, WAD);
        uint160 sp = _sqrt(market);
        uint128 amt = uint128(FullMath.mulDiv(FullMath.mulDiv(v1, 1 << 96, sp), 1 << 96, sp));
        (bool rev,, uint256 swapped) = _tryRec(_callPlace(_floor(market) + 30 - W, PreSwap(true, true, amt), 0));
        assertFalse(rev, "acts");
        assertGt(swapped, 0, "the control's pre-swap fills");
        (uint256 charge,) = _pendingCharge();
        uint256 navA = _navWithFees();
        uint256 realised = navB - navA;
        charge += _settleDueExtra();
        assertLt(charge, realised, "control: the charge (with the markout's extra) falls short of the realised loss");
        console.log("M-5b control: charge, realised loss", charge, realised);
    }
}
