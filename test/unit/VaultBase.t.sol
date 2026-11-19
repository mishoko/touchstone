// SPDX-License-Identifier: MIT
pragma solidity 0.8.26;

import {Vm} from "forge-std/Vm.sol";
import {FullMath} from "@uniswap/v4-core/src/libraries/FullMath.sol";
import {ScriptedVaultBase} from "../utils/ScriptedVault.sol";
import {VaultHarness, Scalars, CheckpointLog} from "../utils/VaultHarness.sol";
import {Obs} from "../../src/types/Obs.sol";
import {Bank, RING} from "../../src/types/Bank.sol";
import {Ladder} from "../../src/libraries/Ladder.sol";
import {ExecLedger} from "../../src/libraries/ExecLedger.sol";
import {Holdings} from "../../src/types/Holdings.sol";
import {TickMath} from "@uniswap/v4-core/src/libraries/TickMath.sol";

/// @notice U-base (spec §3.5): the checkpoint's base accrual on the scripted harness.
contract VaultBaseAccrualTest is ScriptedVaultBase {
    uint256 internal constant WAD = 1e18;

    /// @dev A placed harness (NAV₀ 2e18, idle 1e18/1e18 so NAV at tick 0 is 2e18) with δ₀ and the curator flag set.
    function _h(uint64 delta, bool placed, uint8 mode) internal returns (VaultHarness h) {
        h = _placedHarness();
        Bank memory b = h.bankFull();
        (b.placedByCurator, b.mode) = (placed, mode);
        h.setBank(b);
        h.setScalars(2e18, delta, 0, 1150, 0, 10, 0, 0);
    }

    function _expected(VaultHarness h, uint64 delta, uint256 dtau) internal view returns (uint256) {
        return FullMath.mulDiv(FullMath.mulDiv(h.tier().m_perBlock, 2e18, WAD), uint256(delta) * dtau, WAD);
    }

    function _cohortShares(VaultHarness h) internal view returns (uint256 s) {
        Bank memory b = h.bankFull();
        for (uint256 k; k < RING; ++k) {
            s += b.cohorts[k].shares;
        }
    }

    /// @notice The base amount is `mulDiv(mulDiv(m, NAV₀, WAD), δ₀·Δτ_v, WAD)`, minted as `floor(Base·S/(NAV' − Base))`
    ///         shares into the current epoch's cohort, and emitted in `Checkpoint.base`.
    function test_U_base__accruesOnACuratorPlacedRangeAndMintsAtTheReference() public {
        VaultHarness h = _h(uint64(WAD / 2), true, Ladder.NORMAL);
        uint256 S = h.totalSupply();
        uint256 c0 = _cohortShares(h);
        Obs memory o = _obsIn();
        vm.recordLogs();
        h.checkpoint(o, true, 0);
        uint256 base = _expected(h, uint64(WAD / 2), 60);
        assertGt(base, 0);
        uint256 shares = FullMath.mulDiv(base, S, 2e18 - base);
        assertEq(h.totalSupply() - S, shares, "minted");
        assertEq(_cohortShares(h) - c0, shares, "into the bank's cohort");
        Vm.Log[] memory logs = vm.getRecordedLogs();
        CheckpointLog memory g = abi.decode(logs[logs.length - 1].data, (CheckpointLog));
        assertEq(g.base, base, "Checkpoint.base");
        assertEq(h.scalars().baseOwed, 0);
    }

    /// @notice No base off a curator placement, in EXITED, or at δ₀ = 0 (control: the placed NORMAL vault above).
    function test_U_base__noBaseWithoutAPlacementInExitedOrAtZeroDelta() public {
        VaultHarness[3] memory hs =
            [_h(uint64(WAD / 2), false, Ladder.NORMAL), _h(uint64(WAD / 2), true, Ladder.EXITED), _h(0, true, Ladder.NORMAL)];
        for (uint256 i; i < 3; ++i) {
            uint256 S = hs[i].totalSupply();
            hs[i].checkpoint(_obsIn(), true, 0);
            assertEq(hs[i].totalSupply(), S, "nothing minted");
            assertEq(hs[i].scalars().baseOwed, 0);
        }
        VaultHarness br = _h(uint64(WAD / 2), true, Ladder.BREACHED);
        uint256 S0 = br.totalSupply();
        br.checkpoint(_obsIn(), true, 0);
        assertGt(br.totalSupply(), S0, "BREACHED still accrues");
    }

    /// @notice With no price (stale cache, failed live read) the amount is deferred into `baseOwed`; the next priced
    ///         checkpoint mints it together with its own segment's base.
    function test_U_base__deferredWithoutAPriceThenMinted() public {
        VaultHarness h = _h(uint64(WAD / 2), true, Ladder.NORMAL);
        Obs memory o = _obsIn();
        o.refFresh = false;
        uint256 S = h.totalSupply();
        vm.recordLogs();
        h.checkpoint(o, false, 0);
        uint256 base = _expected(h, uint64(WAD / 2), 60);
        assertEq(h.scalars().baseOwed, base, "deferred");
        assertEq(h.totalSupply(), S, "nothing minted");
        Vm.Log[] memory logs = vm.getRecordedLogs();
        assertEq(logs[0].topics[0], T_DEFERRED, "CheckpointDeferred");
        o.tauV = 120;
        o.refFresh = true;
        h.checkpoint(o, true, 0);
        uint256 total = base + _expected(h, uint64(WAD / 2), 60);
        assertEq(h.totalSupply() - S, FullMath.mulDiv(total, S, 2e18 - total), "owed + new base minted once");
        assertEq(h.scalars().baseOwed, 0);
    }

    /// @notice P5-E-01 (spec §5.1 T16, disclosed): a base deferred during a reference outage is minted at the next
    ///         priced checkpoint on the supply then outstanding, so a holder who leaves in between skips its pro-rata
    ///         part of the dilution and the holders who stay bear it. Two vaults hold the same owed base: in one, half the
    ///         holders left during the outage (half the buckets, half the supply); in the control they leave after the
    ///         mint. The curator receives the owed value either way; the stayers' extra cost is the leaver's share ×
    ///         `baseOwed` (here half of it), the bound the disclosure states.
    function test_U_base__deferredBaseSkipsALeaverDuringTheOutage() public {
        uint256 owed = 1e15;
        uint256[2] memory stayers;
        for (uint256 left; left < 2; ++left) {
            VaultHarness h = _newHarness();
            vm.roll(1200);
            h.setHoldings(left == 1 ? Holdings(5e17, 5e17, 0, -600, 600) : Holdings(1e18, 1e18, 0, -600, 600));
            Bank memory b;
            b.bondShares = 1e21;
            (b.dripBlocks, b.releaseDelay, b.placedByCurator) = (120, 480, true);
            h.setBank(b);
            h.setSupply(left == 1 ? 5e23 : 1e24);
            // the owed base accrued before the leaver left (NAV₀ 2e18); no new segment at this checkpoint (τ unchanged)
            h.setScalars(2e18, uint64(WAD / 2), 60, 1150, uint128(owed), 10, 0, 0);
            uint256 S = h.totalSupply();
            uint256 nav = _navAt(h, 0);
            h.checkpoint(_obsIn(), true, 0);
            assertEq(h.scalars().baseOwed, 0, "minted at the next priced checkpoint");
            uint256 minted = h.totalSupply() - S;
            assertEq(minted, FullMath.mulDiv(owed, S, nav - owed), "on the supply then outstanding");
            // the stayers' value: 5e23 shares of the post-mint vault
            stayers[left] = FullMath.mulDiv(nav, 5e23, h.totalSupply());
        }
        // control (no leaver before the mint): the stayers bear half the owed base; with the leaver gone first, all of it
        assertApproxEqAbs(1e18 - stayers[0], owed / 2, 2, "control: shared with the leaver");
        assertApproxEqAbs(1e18 - stayers[1], owed, 2, "the stayers bear the leaver's part (share x baseOwed)");
    }

    function _navAt(VaultHarness h, int24 t) internal view returns (uint256 v) {
        (v,) = ExecLedger.value(h.holdings(), TickMath.getSqrtPriceAtTick(t));
    }

    /// @notice The mint price: the cached reference when fresh (the live read is not used), else the live read.
    function test_U_base__mintPricedAtTheCacheWhenFreshElseTheLiveRead() public {
        uint256 base;
        for (uint256 fresh; fresh < 2; ++fresh) {
            VaultHarness h = _h(uint64(WAD / 2), true, Ladder.NORMAL);
            Obs memory o = _obsIn();
            o.refTick = 400;
            o.refFresh = fresh == 1;
            uint256 S = h.totalSupply();
            h.checkpoint(o, true, -400);
            base = _expected(h, uint64(WAD / 2), 60);
            uint256 navp = _navAt(h, fresh == 1 ? int24(400) : int24(-400));
            assertEq(h.totalSupply() - S, FullMath.mulDiv(base, S, navp - base), fresh == 1 ? "at the cache" : "at the live read");
        }
        assertTrue(_navAt(_h(0, true, 0), 400) != _navAt(_h(0, true, 0), -400), "control: the two prices differ");
    }

    /// @notice A price that leaves NAV' <= the amount defers it (spec §4.6), with `CheckpointDeferred`.
    function test_U_base__navAtOrBelowTheAmountDefers() public {
        VaultHarness h = _h(uint64(WAD), true, Ladder.NORMAL);
        h.setHoldings(Holdings(1, 1, 0, -600, 600));
        Obs memory o = _obsIn();
        o.tauV = 1e9;
        uint256 S = h.totalSupply();
        vm.recordLogs();
        h.checkpoint(o, true, 0);
        uint256 base = _expected(h, uint64(WAD), 1e9);
        assertGt(base, _navAt(h, 0), "the amount exceeds NAV'");
        assertEq(h.totalSupply(), S, "nothing minted");
        assertEq(h.scalars().baseOwed, base, "deferred");
        Vm.Log[] memory logs = vm.getRecordedLogs();
        assertEq(logs[0].topics[0], T_DEFERRED, "CheckpointDeferred");
    }

    /// @notice The boundary NAV' == amount defers too: a mint there would divide by zero and revert the checkpoint (a
    ///         withdrawal lock), so the comparison is strict (MU-271). Control: one wei less owed mints.
    function test_U_base__navEqualToTheAmountDefersWithoutReverting() public {
        VaultHarness h = _h(0, true, Ladder.NORMAL);
        uint256 navp = _navAt(h, 0);
        h.setScalars(2e18, 0, 0, 1150, uint128(navp), 10, 0, 0);
        uint256 S = h.totalSupply();
        h.checkpoint(_obsIn(), true, 0);
        assertEq(h.totalSupply(), S, "nothing minted at NAV' == amount");
        assertEq(h.scalars().baseOwed, navp, "still owed");
        VaultHarness c = _h(0, true, Ladder.NORMAL);
        c.setScalars(2e18, 0, 0, 1150, uint128(navp - 1), 10, 0, 0);
        c.checkpoint(_obsIn(), true, 0);
        assertGt(c.totalSupply(), S, "control: below NAV' it mints");
    }
}
