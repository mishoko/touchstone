// SPDX-License-Identifier: MIT
pragma solidity 0.8.26;

import {Test} from "forge-std/Test.sol";
import {Tier} from "../../src/types/Tier.sol";
import {Tiers} from "../../script/Tiers.sol";

contract TiersHarness {
    function check(Tier memory t, uint256 blocktime) external pure {
        Tiers.check(t, blocktime);
    }
}

contract TiersTest is Test {
    TiersHarness h = new TiersHarness();

    function test_tiers_allPassChecks() public view {
        (Tier memory t, uint256 bt) = Tiers.mainnet();
        h.check(t, bt);
        (t, bt) = Tiers.demo();
        h.check(t, bt);
        (t, bt) = Tiers.testing();
        h.check(t, bt);
    }

    function test_tiers_cureBoundIsTight() public {
        // TEST: EDGE_DELAY 20 + 2*GRID 40 + REF_WINDOW/blocktime 50 + GRID 20 = 130.
        (Tier memory t, uint256 bt) = Tiers.testing();
        t.CURE_BLOCKS = 130;
        h.check(t, bt);
        t.CURE_BLOCKS = 129;
        vm.expectRevert(abi.encodeWithSelector(Tiers.TierInvalid.selector, "CURE_BLOCKS"));
        h.check(t, bt);
    }

    function test_tiers_resetRunAtLeastTwo() public {
        (Tier memory t, uint256 bt) = Tiers.demo();
        t.RESET_RUN = 1;
        vm.expectRevert(abi.encodeWithSelector(Tiers.TierInvalid.selector, "RESET_RUN"));
        h.check(t, bt);
    }

    function test_tiers_gridTimesBlocktimeIsGridSec() public {
        (Tier memory t, uint256 bt) = Tiers.mainnet();
        vm.expectRevert(abi.encodeWithSelector(Tiers.TierInvalid.selector, "GRID_SEC"));
        h.check(t, bt + 1);
    }

    function test_tiers_epochIsWholeGrids() public {
        (Tier memory t, uint256 bt) = Tiers.testing();
        t.EPOCH = 130;
        vm.expectRevert(abi.encodeWithSelector(Tiers.TierInvalid.selector, "EPOCH"));
        h.check(t, bt);
    }

    function test_tiers_dripIsOneEpoch() public {
        (Tier memory t, uint256 bt) = Tiers.testing();
        t.FORFEIT_DRIP_BLOCKS = t.EPOCH - 1;
        vm.expectRevert(abi.encodeWithSelector(Tiers.TierInvalid.selector, "FORFEIT_DRIP_BLOCKS"));
        h.check(t, bt);
    }

    function test_tiers_cooldownOutlastsMarkoutWindow() public {
        // TEST: COOLDOWN 12 blocks of 12 s = 144 s = H_MARKOUT 120 s + 2 blocks passes; 11 fails.
        (Tier memory t, uint256 bt) = Tiers.testing();
        t.COOLDOWN = 12;
        h.check(t, bt);
        t.COOLDOWN = 11;
        vm.expectRevert(abi.encodeWithSelector(Tiers.TierInvalid.selector, "COOLDOWN"));
        h.check(t, bt);
    }

    function test_tiers_markoutParameters() public pure {
        (Tier memory m,) = Tiers.mainnet();
        (Tier memory d,) = Tiers.demo();
        (Tier memory t,) = Tiers.testing();
        assertEq(m.BOND_RATIO, 1.74e15);
        assertEq(m.RESERVE_RATIO, 6e14);
        assertEq(m.BOND_MIN, 1.74e18);
        assertEq(d.BOND_MIN, 1.74e18);
        assertEq(t.BOND_MIN, 0);
        assertEq(m.NAV_MAX, 1850e18);
        assertEq(d.NAV_MAX, 1850e18);
        assertEq(t.NAV_MAX, type(uint128).max);
        assertEq(m.H_MARKOUT, 120);
        assertEq(d.H_MARKOUT, 20);
        assertEq(t.H_MARKOUT, 120);
        assertEq(m.EDGE_DELAY, 3600);
        assertEq(t.COOLDOWN, 14);
    }

    function test_tiers_matchParameterTable() public pure {
        (Tier memory m,) = Tiers.mainnet();
        (Tier memory d,) = Tiers.demo();
        (Tier memory t,) = Tiers.testing();
        assertEq(m.CURE_BLOCKS, 7200);
        assertEq(d.CURE_BLOCKS, 1800);
        assertEq(t.CURE_BLOCKS, 200);
        assertEq(m.EPOCH, 50_400);
        assertEq(d.EPOCH, 3600);
        assertEq(t.EPOCH, 120);
        assertEq(m.FORFEIT_DRIP_BLOCKS, m.EPOCH);
        assertEq(d.FORFEIT_DRIP_BLOCKS, d.EPOCH);
        assertEq(t.FORFEIT_DRIP_BLOCKS, t.EPOCH);
        assertEq(m.REFRESH_GAS_MIN, uint32(uint256(m.REF_GAS_CAP) * 64 / 63 + 30_000));
        assertEq(t.m_perBlock, 3_805_175_038); // 1e16 * 12 / 31,536,000 (the reference model's TEST tier)
    }

    /// @notice F-061 (s29 freeze): the base rate per block on every tier equals spec §6's per-chain `m_perBlock` row:
    ///         2.2%/yr on MAINNET (12 s) and DEMO (1 s), 1%/yr on TEST (a test-tier value). Any edit to m fails here.
    function test_tiers_baseRateIsTheFrozenSpecValue() public pure {
        (Tier memory m,) = Tiers.mainnet();
        (Tier memory d,) = Tiers.demo();
        (Tier memory t,) = Tiers.testing();
        assertEq(m.m_perBlock, 8_371_385_083, "MAINNET: 2.2e16 * 12 / 31,536,000");
        assertEq(d.m_perBlock, 697_615_423, "DEMO: 2.2e16 / 31,536,000");
        assertEq(t.m_perBlock, 3_805_175_038, "TEST: 1e16 * 12 / 31,536,000");
    }
}
