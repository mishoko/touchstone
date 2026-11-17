// SPDX-License-Identifier: MIT
pragma solidity 0.8.26;

import {Vm} from "forge-std/Vm.sol";
import {console} from "forge-std/console.sol";
import {CellBase} from "./CellBase.sol";
import {ITouchstoneHook} from "../../src/interfaces/ITouchstoneHook.sol";

/// @notice M-N1 (spec §5.1 N1, §3.5): pushing or parking the pool price to move pay. The execution charge is zero
///         between actions, and the base's τ_v needs the pool price strictly inside the range AND the cached reference
///         in range. The cell runs at the tier's real m and differences every attack against the no-attack world
///         (the same blocks and checkpoints, no push). The coalition's wealth is the Sybil's tokens plus the curator's
///         shares (base minted into the bank included) at the market price; both branches end at the same pool price
///         and collect the vault's fees before measuring.
///         - Lane A, the reference out of range (the market left by 1, 5, 10 and 50%, either side): the Sybil parks
///           the pool one unit inside the near edge or mid-range for 3 grids. Pass: no τ_v and no base difference,
///           net ≤ 0, and no curator action (the charge is unchanged: none in either branch).
///         - Lane B, the parking lane (disclosed): the market has just left the range and the cached reference still
///           reads in range. Pass: the τ_v credit is at most the blocks the cached reference stayed in range (reported
///           against GRID·(1 + ⌈REF_WINDOW/GRID_SEC⌉), which is 2·GRID on the
///           mainnet and DEMO tiers); positive control: the credit and the base it mints are > 0 at the tier's m.
contract MN1Test is CellBase {
    bytes32 internal constant T_ACTION = keccak256("Action(uint8,uint256,uint160,uint256,uint256)");

    struct Run {
        int256 wealth;
        uint64 tauV;
        uint256 cur;
        uint256 inRangeCache;
    }

    function _cellM(uint64 m) internal pure override returns (uint64) {
        return m;
    }

    /// @notice One branch: optionally park at `park`, `dt` observed blocks (a refresh each block), a checkpoint, then
    ///         back to `back` and a second checkpoint in the same block (collects the fees of the way back; Δτ_v = 0).
    function _run(bool attack, uint160 park, uint160 back, uint256 dt) internal returns (Run memory r) {
        int256 w0 = _wealth();
        uint64 t0 = ITouchstoneHook(hook).obs().tauV;
        uint256 c0 = _curatorShares();
        (int24 lo, int24 hi) = (_h().lo, _h().hi);
        vm.recordLogs();
        if (attack) syb.pushTo(park);
        for (uint256 i; i < dt; ++i) {
            // a span is credited with the cache it started from: count it before this block's refresh
            int24 c = ITouchstoneHook(hook).obs().refTick;
            if (lo <= c && c < hi) ++r.inRangeCache;
            advance(1);
            ITouchstoneHook(hook).refreshRef();
        }
        v.settle();
        if (attack) syb.pushTo(back);
        v.settle();
        Vm.Log[] memory logs = vm.getRecordedLogs();
        for (uint256 i; i < logs.length; ++i) {
            assertTrue(logs[i].topics.length == 0 || logs[i].topics[0] != T_ACTION, "no curator action, no charge");
        }
        r.wealth = _wealth() - w0;
        r.tauV = ITouchstoneHook(hook).obs().tauV - t0;
        r.cur = _curatorShares() - c0;
    }

    function _pair(uint160 park, uint160 back, uint256 dt) internal returns (Run memory b, Run memory a) {
        uint256 s = vm.snapshotState();
        b = _run(false, park, back, dt);
        vm.revertToState(s);
        a = _run(true, park, back, dt);
        vm.revertToState(s);
    }

    function _cacheIn(int24 lo, int24 hi) internal view returns (bool) {
        int24 c = ITouchstoneHook(hook).obs().refTick;
        return lo <= c && c < hi;
    }

    function _d() internal pure returns (int24[4] memory) {
        return [int24(100), 488, 953, 4055]; // 1, 5, 10 and 50% in price
    }

    function test_M_N1__pushOrParkWithTheReferenceOutEarnsNothingAndLoses() public {
        (int24 lo, int24 hi) = (_h().lo, _h().hi);
        int24[4] memory d = _d();
        for (uint256 up; up < 2; ++up) {
            for (uint256 i; i < 4; ++i) {
                uint256 snap = vm.snapshotState();
                int24 r = up == 1 ? hi + d[i] : lo - d[i];
                _moveMarket(r);
                for (uint256 j; j < 6 && _cacheIn(lo, hi); ++j) {
                    advance(tier.GRID);
                    ITouchstoneHook(hook).refreshRef();
                }
                assertFalse(_cacheIn(lo, hi), "the cached reference reads out of range");
                (uint160 start,) = _spot();
                uint160[2] memory parks = [up == 1 ? _sqrt(hi) - 1 : _sqrt(lo) + 1, _sqrt((lo + hi) / 2)];
                for (uint256 k; k < 2; ++k) {
                    (Run memory b, Run memory a) = _pair(parks[k], start, 3 * tier.GRID);
                    assertEq(a.tauV, b.tauV, "no tau_v credit with the reference out");
                    assertEq(a.cur, b.cur, "no base difference");
                    int256 net = a.wealth - b.wealth;
                    console.log("M-N1 lane A up/pct-index/park", up, i, k);
                    console.logInt(net);
                    assertLe(net, 1, "the push loses");
                }
                vm.revertToState(snap);
            }
        }
    }

    function test_M_N1__parkingWhileTheCacheLagsIsBoundedByItsInRangeTime() public {
        (int24 lo, int24 hi) = (_h().lo, _h().hi);
        int24[4] memory d = _d();
        for (uint256 i; i < 4; ++i) {
            uint256 snap = vm.snapshotState();
            // the market jumps above the range and a third party takes the pool there; the cache still reads in range
            market = hi + d[i];
            refPool.setTick(market);
            _thirdPartyPush(_sqrt(market));
            assertTrue(ITouchstoneHook(hook).obs().refTick < hi, "the cached reference lags in range");
            (uint160 start,) = _spot();
            (Run memory b, Run memory a) = _pair(_sqrt(hi) - 1, start, 4 * tier.GRID);
            uint256 credit = a.tauV - b.tauV;
            console.log("M-N1 lane B pct-index, tau_v credit, cache-in-range blocks, 2*GRID", i, credit, a.inRangeCache);
            console.logInt(a.wealth - b.wealth);
            assertLe(credit, a.inRangeCache, "credited only while the cached reference reads in range");
            // one anchor plus the anchors the TWAP needs to cross the edge: GRID * (1 + ceil(REF_WINDOW / GRID_SEC)),
            // which is the disclosed 2*GRID on the mainnet and DEMO tiers (REF_WINDOW <= GRID_SEC)
            uint256 lagAnchors = (uint256(tier.REF_WINDOW) + tier.GRID_SEC - 1) / tier.GRID_SEC;
            assertLe(credit, tier.GRID * (1 + lagAnchors), "within one anchor plus the TWAP's lag");
            // positive control: parking earns tau_v and base at the tier's m
            assertGt(credit, 0, "control: parking earns tau_v");
            assertGt(a.cur, b.cur, "control: and base");
            vm.revertToState(snap);
        }
    }
}
