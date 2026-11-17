// SPDX-License-Identifier: MIT
pragma solidity 0.8.26;

import {console} from "forge-std/console.sol";
import {CellBase} from "./CellBase.sol";
import {TouchstoneVault} from "../../src/TouchstoneVault.sol";
import {VaultView} from "../utils/VaultView.sol";

using VaultView for TouchstoneVault;

/// @notice M-N4 and M-N4edge (spec §5.1 N4, N4-edge): the curator's Sybil pushes spot (up to the fence), the curator
///         places with no swap at the pushed price, and the Sybil unwinds through the vault's new liquidity. The 100%
///         worse-of charge (spot, the pre-move spot, the reference) and the markout settlement make the coalition
///         lose. Positive control: the same attack against a reference that follows the push (the thin-pool premise)
///         profits.
contract MN4Test is CellBase {
    /// @notice Fuzz: state, push (both sides, up to the fence and 10 ticks past it), range, third-party depth.
    function test_M_N4__selfSandwichAroundANoSwapPlacementLoses(uint8 st, int24 d, uint256 loSeed, uint8 kIdx) public {
        _pickState(st);
        d = int24(bound(d, -70, 70));
        _thirdPartyDepth(KS[kIdx % 5]);
        int24 s = market + d;
        Out memory o = _attackPlace(_sqrt(s), _legalLo(s, market, loSeed), _none(), 0);
        if (d > 60 || d < -60) assertTrue(o.reverted, "past the fence the placement reverts");
        _assertLoses(o, "M-N4");
    }

    /// @notice The grid (states × pushes 20, 30, 100 (= 4·FEE_TICKS and beyond the fence) × both sides × depths × the
    ///         two extreme legal ranges): every row loses; at least one row per state acts and is charged.
    function test_M_N4__gridLoses() public {
        int24[4] memory ds = [int24(20), 30, 60, 100];
        for (uint256 st; st < 3; ++st) {
            uint256 snap = vm.snapshotState();
            _pickState(st);
            uint256 acted;
            int256 worst = type(int256).min;
            uint256 snap2 = vm.snapshotState();
            for (uint256 i; i < 4; ++i) {
                for (uint256 sg; sg < 2; ++sg) {
                    for (uint256 k; k < 5; k += 2) {
                        for (uint256 e; e < 2; ++e) {
                            int24 d = sg == 0 ? ds[i] : -ds[i];
                            _thirdPartyDepth(KS[k]);
                            int24 s = market + d;
                            Out memory o = _attackPlace(_sqrt(s), _legalLo(s, market, e == 0 ? 0 : type(uint256).max / 2), _none(), 0);
                            _assertLoses(o, "M-N4 grid");
                            if (!o.reverted && o.charge0 > 0) ++acted;
                            if (o.net > worst) worst = o.net;
                            vm.revertToState(snap2);
                        }
                    }
                }
            }
            console.log("M-N4 state", st, "rows acted and charged", acted);
            console.log("  max net (wei)", worst);
            assertGt(acted, 20, "non-vacuity: the rows reach the charge");
            vm.revertToState(snap);
        }
    }

    /// @notice Fuzz (M-N4edge): the push to one unit inside an edge, the one-sided placement on that edge.
    function test_M_N4edge__selfSandwichAroundAnEdgePlacementLoses(uint8 st, int24 d, bool upper, uint8 kIdx) public {
        _pickState(st);
        d = int24(bound(d, -60, 60));
        _thirdPartyDepth(KS[kIdx % 5]);
        (uint160 ps, int24 lo) = _edge(d, upper);
        Out memory o = _attackPlace(ps, lo, _none(), 0);
        _assertLoses(o, "M-N4edge");
    }

    /// @notice Positive control (M-N4): the reference follows the push (spot only, no markout information). The
    ///         exit-state self-sandwich at depth 0 then profits.
    function test_M_N4__ctlReferenceFollowingThePushProfits() public {
        _pickState(1);
        int24 s = market - 60;
        _refFollows(s);
        Out memory o = _attackPlace(_sqrt(s), _legalLo(s, s, 0), _none(), 0);
        assertFalse(o.reverted, "the control acts");
        assertGt(o.net, 0, "control: the attack profits");
        console.log("M-N4 control net (wei)", o.net);
    }

    /// @notice Positive control (M-N4edge): the same with the reference following an edge push.
    function test_M_N4edge__ctlReferenceFollowingThePushProfits() public {
        _pickState(1);
        (uint160 ps, int24 lo) = _edge(60, true);
        _refFollows(market + 60);
        Out memory o = _attackPlace(ps, lo, _none(), 0);
        assertFalse(o.reverted, "the control acts");
        assertGt(o.net, 0, "control: the attack profits");
        console.log("M-N4edge control net (wei)", o.net);
    }
}
