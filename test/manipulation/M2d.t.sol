// SPDX-License-Identifier: MIT
pragma solidity 0.8.26;

import {console} from "forge-std/console.sol";
import {CellBase} from "./CellBase.sol";
import {CuratorBundler} from "../../src/periphery/CuratorBundler.sol";
import {TouchstoneVault} from "../../src/TouchstoneVault.sol";
import {PreSwap} from "../../src/types/PreSwap.sol";

/// @notice M-2d (spec §5.1 F-015 / R2d-02, F-024): a push of the pool's price and a placement at the pushed level.
///         - One-shot lane: exit states, pushes up to the fence, every legal range and both edge targets, third-party
///           depths 0–3× the vault's liquidity.
///         - Lag lane: the reference's TWAP lags the market (it reads `market − lag` over its window); the colluder
///           pushes spot anywhere within the fence of the lagging reference and places; the markout window reads the
///           market. The markout legs settle what the immediate legs miss.
///         - Grief lane: an outside party pushes ahead of an honest curator, who re-aligns spot to the reference
///           through its bundler (tolerance 10 ticks) and places; the griefer must lose.
///         Positive controls: the reference follows the push (no fence, spot only: the guard-off lane), and the honest
///         curator placing in place with no re-alignment.
contract M2dTest is CellBase {
    /// @notice One-shot lane (fuzz): exit states, a push of 20–60 ticks either side, an in-range legal range or an edge
    ///         target, a third-party depth.
    function test_M_2d__oneShotPushAndPlacementLoses(uint8 st, int24 d, uint256 loSeed, uint8 mode, uint8 kIdx)
        public
    {
        _pickState(1 + st % 2);
        d = int24(bound(d, 20, 60));
        if (mode & 1 == 1) d = -d;
        _thirdPartyDepth(KS[kIdx % 5]);
        Out memory o;
        if (mode % 3 == 0) {
            int24 s = market + d;
            o = _attackPlace(_sqrt(s), _legalLo(s, market, loSeed), _none(), 0);
        } else {
            (uint160 ps, int24 lo) = _edge(d, mode % 3 == 2);
            o = _attackPlace(ps, lo, _none(), 0);
        }
        _assertLoses(o, "M-2d one-shot");
    }

    /// @notice Lag lane (fuzz): the reference lags the market by up to 60 ticks either side; spot pushed anywhere within
    ///         the fence of the lagging reference; any legal range; the markout at the market.
    function test_M_2d__lagLanePushToTheFenceLoses(uint8 st, int24 lag, int24 d, uint256 loSeed, uint8 kIdx) public {
        _pickState(st);
        lag = int24(bound(lag, -60, 60));
        d = int24(bound(d, -60, 60));
        _thirdPartyDepth(KS[kIdx % 5]);
        int24 ref = market - lag;
        _lagReference(ref);
        int24 s = ref + d;
        Out memory o = _attackPlace(_sqrt(s), _legalLo(s, ref, loSeed), _none(), 0);
        _assertLoses(o, "M-2d lag");
    }

    /// @notice The lag lane's grid: lags ±20, ±40, ±60 × pushes to both fence edges × both extreme ranges, in the three
    ///         states; every row loses and the rows act.
    function test_M_2d__lagLaneGridLoses() public {
        int24[3] memory ls = [int24(20), 40, 60];
        uint256 acted;
        for (uint256 st; st < 3; ++st) {
            uint256 snap = vm.snapshotState();
            _pickState(st);
            uint256 snap2 = vm.snapshotState();
            for (uint256 i; i < 3; ++i) {
                for (uint256 sg; sg < 4; ++sg) {
                    for (uint256 e; e < 2; ++e) {
                        int24 lag = sg % 2 == 0 ? ls[i] : -ls[i];
                        int24 ref = market - lag;
                        _lagReference(ref);
                        int24 s = ref + (sg < 2 ? int24(60) : int24(-60));
                        Out memory o = _attackPlace(_sqrt(s), _legalLo(s, ref, e == 0 ? 0 : 1e9), _none(), 0);
                        _assertLoses(o, "M-2d lag grid");
                        if (!o.reverted && o.charge0 + o.extra > 0) ++acted;
                        vm.revertToState(snap2);
                    }
                }
            }
            vm.revertToState(snap);
        }
        console.log("M-2d lag grid: rows acted and charged", acted, "of 72");
        assertGe(acted, 24, "non-vacuity");
    }

    /// @dev The reference reads `ref` over its TWAP window up to now and the market from the next block.
    function _lagReference(int24 ref) internal {
        uint32[] memory st = new uint32[](2);
        int24[] memory tk = new int24[](2);
        (st[0], tk[0]) = (uint32(vm.getBlockTimestamp() - 2 hours), ref);
        (st[1], tk[1]) = (uint32(vm.getBlockTimestamp() + 1), market);
        refPool.setPath(st, tk);
    }

    /// @notice Positive control (the guard-off lane): the reference follows a push of 1,000 ticks (no fence, spot only);
    ///         the placement at the pushed level and the unwind profit.
    function test_M_2d__ctlGuardOffProfits() public {
        _pickState(1);
        int24 s = market - 1000;
        _refFollows(s);
        Out memory o = _attackPlace(_sqrt(s), _midLo(s, s), _none(), 0);
        assertFalse(o.reverted, "the control acts");
        assertGt(o.net, 0, "control: the attack profits");
        console.log("M-2d guard-off control net (wei)", o.net);
    }

    // ------------------------------------------------------------------ the grief lane

    /// @notice Grief lane (fuzz): an outside party pushes up to 60 ticks ahead of an honest curator's `moveAndPlace`
    ///         (re-alignment to the reference, tolerance 10); the curator's call reverts `Tolerance` past 10, else
    ///         re-aligns and places. The griefer (alone: not the curator's coalition) loses.
    function test_M_2d__griefAgainstAnHonestCuratorLoses(uint8 st, int24 d, uint8 kIdx) public {
        _pickState(st);
        d = int24(bound(d, -60, 60));
        _thirdPartyDepth(KS[kIdx % 5]);
        Out memory o = _grief(d, true);
        if (d > 10 || d < -10) assertEq(o.err, CuratorBundler.Tolerance.selector, "past the tolerance");
        assertLe(o.sybil, 1, "M-2d grief: the griefer must lose");
    }

    /// @notice Positive control: the honest curator places in place at the pushed spot (no re-alignment, no tolerance);
    ///         the griefer's unwind through the new liquidity profits.
    function test_M_2d__ctlGriefWithoutReAlignmentProfits() public {
        _pickState(1);
        // the gain grows with d² and the fees paid with d: break-even near 10 ticks, so the control pushes 60
        Out memory o = _grief(-60, false);
        assertFalse(o.reverted, "the control acts");
        assertGt(o.sybil, 0, "control: the griefer profits");
        console.log("M-2d grief control: griefer net (wei)", o.sybil);
    }

    function _grief(int24 d, bool realign) internal returns (Out memory o) {
        v.settle();
        int256 w0 = _wealth();
        int256 s0 = _sybilValue();
        uint256 c0 = _curatorShares();
        int24 s = market + d;
        syb.pushTo(_sqrt(s));
        int24 lo = _midLo(market, market);
        bytes memory call = realign
            ? abi.encodeCall(
                CuratorBundler.moveAndPlace, (_sqrt(market), 10, lo, lo + W, _none(), uint160(0), type(uint256).max)
            )
            : abi.encodeCall(CuratorBundler.setPositions, (_midLo(s, market), _midLo(s, market) + W, _none(), 0, type(uint256).max));
        (o.reverted, o.err) = _try(call);
        syb.pushTo(_sqrt(market));
        o.extra = _settleDueExtra();
        _close(o, w0, s0, c0);
    }
}
