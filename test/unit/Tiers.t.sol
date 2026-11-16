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
}
