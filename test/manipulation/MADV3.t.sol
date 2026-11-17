// SPDX-License-Identifier: MIT
pragma solidity 0.8.26;

import {console} from "forge-std/console.sol";
import {TickMath} from "@uniswap/v4-core/src/libraries/TickMath.sol";
import {FullMath} from "@uniswap/v4-core/src/libraries/FullMath.sol";
import {LiquidityAmounts} from "@uniswap/v4-periphery/src/libraries/LiquidityAmounts.sol";
import {CellBase} from "./CellBase.sol";
import {VaultHarness} from "../utils/VaultHarness.sol";
import {TouchstoneVault} from "../../src/TouchstoneVault.sol";
import {ITouchstoneHook} from "../../src/interfaces/ITouchstoneHook.sol";
import {VaultView} from "../utils/VaultView.sol";
import {PreSwap} from "../../src/types/PreSwap.sol";

using VaultView for TouchstoneVault;

/// @notice M-ADV3 (spec §5.1 ADV-3): JIT self-dealing on the curator's own swap. The Sybil adds narrow liquidity on the
///         swap's path (optionally after a push within the fence), the curator swaps from idle (`rebalanceSwap`, or a
///         placement's pre-swap after the old position is removed), the Sybil removes the JIT and unwinds spot. The
///         100% worse-of charge (spot read first, the reference) and the markout make the coalition lose. Positive
///         control: a reference that follows the push.
contract MADV3Test is CellBase {
    /// @dev The swap's amount for `frac` (WAD) of NAV at the market: in token0 when `z`.
    function _amount(bool z, uint256 frac) internal view returns (uint128) {
        uint256 nav = _nav(_sqrt(market));
        uint256 v1 = FullMath.mulDiv(nav, frac, WAD);
        if (!z) return uint128(v1);
        uint160 sp = _sqrt(market);
        return uint128(FullMath.mulDiv(FullMath.mulDiv(v1, 1 << 96, sp), 1 << 96, sp));
    }

    /// @dev The JIT range on the swap's path from `tickSpot` to its limit `FEE_TICKS + BAND_SWAP` away.
    function _jitRange(bool z, int24 tickSpot) internal view returns (int24 jlo, int24 jhi) {
        int24 slip = tier.FEE_TICKS + tier.BAND_SWAP;
        return z ? (_floor(tickSpot - slip), _floor(tickSpot) + TICK_SPACING) : (_floor(tickSpot), _floor(tickSpot + slip) + TICK_SPACING);
    }

    /// @notice Fuzz over the state, the push (within the fence), the JIT depth, the side, the size (up to
    ///         `MAX_SWAP_ACTION` and past it) and the third-party depth.
    function test_M_ADV3__jitOnTheCuratorsSwapLoses(uint8 st, int24 d, uint256 J, bool z, uint256 frac, uint8 kIdx)
        public
    {
        _pickState(st);
        d = int24(bound(d, -60, 60));
        J = bound(J, 0, 1e26);
        frac = bound(frac, 1e14, 2.5e16);
        _thirdPartyDepth(KS[kIdx % 5]);
        (int24 jlo, int24 jhi) = _jitRange(z, market + d);
        Out memory o = _attackSwap(d == 0 ? 0 : _sqrt(market + d), jlo, jhi, J, z, _amount(z, frac));
        _assertLoses(o, "M-ADV3 rebalanceSwap");
    }

    /// @notice The same JIT on a placement's pre-swap (the old position removed first), in both exit states.
    function test_M_ADV3__jitOnAPlacementsPreSwapLoses(uint8 st, int24 d, uint256 J, bool z, uint256 frac) public {
        _pickState(1 + st % 2);
        d = int24(bound(d, -50, 50));
        J = bound(J, 1e18, 1e26);
        frac = bound(frac, 1e14, 2e16);
        int24 s = market + d;
        (jitLo, jitHi) = _jitRange(z, s);
        jitL = J;
        Out memory o = _attackPlace(d == 0 ? 0 : _sqrt(s), _midLo(s, market), PreSwap(true, z, _amount(z, frac)), 0);
        _assertLoses(o, "M-ADV3 pre-swap");
    }

    /// @notice The grid (Python's lanes): push 0, 20, 30, 60 × JIT 1e20, 1e22, 1e24 × both sides × 0.5% and 2% of NAV,
    ///         in range and in both exit states; every row loses and the rows act.
    function test_M_ADV3__gridLoses() public {
        int24[4] memory ds = [int24(0), 20, 30, 60];
        uint256[3] memory js = [uint256(1e20), 1e22, 1e24];
        uint256[2] memory fs = [uint256(5e15), 2e16];
        uint256 acted;
        for (uint256 st; st < 3; ++st) {
            uint256 snap = vm.snapshotState();
            _pickState(st);
            uint256 snap2 = vm.snapshotState();
            for (uint256 i; i < 4; ++i) {
                for (uint256 j; j < 3; ++j) {
                    for (uint256 zi; zi < 2; ++zi) {
                        for (uint256 f; f < 2; ++f) {
                            bool z = zi == 0;
                            // the push against the swap's direction (the vault then swaps at a pushed price)
                            int24 d = z ? -ds[i] : ds[i];
                            (int24 jlo, int24 jhi) = _jitRange(z, market + d);
                            Out memory o =
                                _attackSwap(d == 0 ? 0 : _sqrt(market + d), jlo, jhi, js[j], z, _amount(z, fs[f]));
                            _assertLoses(o, "M-ADV3 grid");
                            if (!o.reverted && o.charge0 > 0) ++acted;
                            vm.revertToState(snap2);
                        }
                    }
                }
            }
            vm.revertToState(snap);
        }
        // state 0 (the deployment's placement) holds no idle to swap from: its rows revert SwapCap (from idle only)
        console.log("M-ADV3 grid: rows acted and charged", acted, "of 144");
        assertGe(acted, 48, "non-vacuity: the exit states' rows reach the charge");
    }

    /// @notice Positive control: the reference follows a 60-tick push; the vault buys the pushed token from the JIT and
    ///         the Sybil unwinds: the coalition profits.
    function test_M_ADV3__ctlReferenceFollowingThePushProfits() public {
        _pickState(2);
        int24 s = market + 60;
        _refFollows(s);
        (int24 jlo, int24 jhi) = _jitRange(false, s);
        Out memory o = _attackSwap(_sqrt(s), jlo, jhi, 1e22, false, _amount(false, 2e16));
        assertFalse(o.reverted, "the control acts");
        assertGt(o.net, 0, "control: the attack profits");
        console.log("M-ADV3 control net (wei)", o.net);
    }
}

/// @notice M-ADV3-move (owner-items panel D2): the vault's own move through the curator's JIT liquidity. The Sybil adds
///         liquidity on `[ref, ref + 60)`, the curator places with `moveTo = sqrtAt(ref + 60)` (the vault buys through
///         the JIT, up to the fence), and the market then moves at least 12 ticks against the vault's purchase over the
///         markout window (scripted). The move shares the per-action swap cap with the pre-swap and counts toward the
///         epoch cap, so it reverts `SwapCap` or the coalition loses after the settlement. The net is the coalition's
///         wealth against the same market path with no attack (the counterfactual), valued at the final market price.
abstract contract MoveRows is CellBase {
    /// @dev The JIT liquidity on `[c, c + 60)` whose token0 is worth `frac` of NAV at the market.
    function _jitFor(uint256 frac) internal view returns (uint128) {
        uint256 v1 = FullMath.mulDiv(_nav(_sqrt(market)), frac, WAD);
        // the token1 the vault pays to cross [c, c + 60) is ≈ v1
        return LiquidityAmounts.getLiquidityForAmount1(_sqrt(market), _sqrt(market + 60), v1);
    }

    /// @dev The scripted market after the action: `drop` ticks below the reference from the next block on.
    function _dropAfter(int24 drop) internal {
        uint32[] memory st = new uint32[](2);
        int24[] memory tk = new int24[](2);
        (st[0], tk[0]) = (uint32(vm.getBlockTimestamp() - 2 hours), market);
        (st[1], tk[1]) = (uint32(vm.getBlockTimestamp() + 1), market - drop);
        refPool.setPath(st, tk);
    }

    struct MoveOut {
        bool reverted;
        bytes4 err;
        int256 net;
        int256 depositorLoss;
        uint256 charge0;
        uint256 reserve;
        uint256 extra;
        int256 sybilDelta;
        int24 lo;
    }

    function _aliceValue() internal view returns (int256) {
        return int256(FullMath.mulDiv(v.balanceOf(alice), _nav(_sqrt(market)), v.totalSupply()));
    }

    /// @notice The path common to the attack and its counterfactual: the market drops `drop` ticks, an arbitrageur
    ///         (the Sybil) moves the pool's spot there, the markout settles.
    function _afterPath(int24 drop) internal returns (uint256 extra) {
        int24 c = market;
        market = c - drop;
        syb.pushTo(_sqrt(market));
        extra = _settleDueExtra();
        v.settle();
    }

    /// @dev The counterfactual: no JIT and no action over the same market path (the charge's own counterfactual,
    ///      NAV_before); the state is restored after. Returns the Sybil's value, the curator's shares and the depositor's
    ///      value at its end.
    function _baseline(int24 drop) internal returns (int256 sB, uint256 cB, int256 aB) {
        uint256 snap = vm.snapshotState();
        int24 c = market;
        _dropAfter(drop);
        _afterPath(drop);
        (sB, cB, aB) = (_sybilValue(), _curatorShares(), _aliceValue());
        vm.revertToState(snap);
        market = c;
    }

    function _moveAttack(uint256 frac, int24 drop) internal returns (MoveOut memory o) {
        return _moveAttackAt(frac, drop, _midLo(market + 60, market));
    }

    function _moveAttackAt(uint256 frac, int24 drop, int24 lo) internal returns (MoveOut memory o) {
        v.settle();
        int24 c = market;
        uint128 J = _jitFor(frac);
        (int256 sB, uint256 cB, int256 aB) = _baseline(drop);
        // the attack
        _dropAfter(drop);
        syb.lp(c, c + 60, int256(uint256(J)));
        (o.reverted, o.err) = _try(_callPlace(lo, _none(), _sqrt(c + 60)));
        if (!o.reverted) (o.charge0, o.reserve) = _pendingCharge();
        syb.lp(c, c + 60, -int256(uint256(J)));
        o.extra = _afterPath(drop);
        o.sybilDelta = _sybilValue() - sB;
        o.net = o.sybilDelta - _burnedValue(cB, _curatorShares());
        o.depositorLoss = aB - _aliceValue();
        o.lo = lo;
    }
}

contract MADV3MoveTest is MoveRows {
    /// @notice Row 1 at a 12-tick markout (fuzz; the registered row asks for every markout of at least 12 ticks, which
    ///         fails from ≈ 70: see the escalated scan): one move through JIT worth up to 10% of NAV, any legal range: a guard reverts (`SwapCap`; `DeltaMin` for a range the
    ///         post-move inventory cannot fill), or the coalition loses and the depositors lose at most the reserve.
    function test_M_ADV3_move__oneMoveRevertsSwapCapOrLoses(uint256 frac, uint256 loSeed) public {
        frac = bound(frac, 1e15, 1e17);
        _checkMove(_moveAttackAt(frac, 12, _legalLo(market + 60, market, loSeed)));
    }

    function _checkMove(MoveOut memory o) internal pure {
        if (o.reverted) {
            assertTrue(
                o.err == TouchstoneVault.SwapCap.selector || o.err == TouchstoneVault.DeltaMin.selector,
                "only a guard reverts the move"
            );
        } else {
            assertLe(o.net, 1, "M-ADV3-move: the attack must lose money");
            assertLe(o.depositorLoss, int256(o.reserve), "the depositors lose at most the reserve");
        }
    }

    /// @notice Row 1 over markouts of 12–120 ticks, the mean of the draw against the purchase and its mirror. Weak
    ///         evidence (fix review S14-01): the up draw's loss is dominated by the arbitrage the counterfactual credits to
    ///         the Sybil. Kept as a regression row; it is not the registered rule.
    function test_M_ADV3_move__oneMoveLosesInExpectationOverMirroredMarkouts(uint256 frac, int24 drop, uint256 loSeed)
        public
    {
        frac = bound(frac, 1e15, 1e17);
        drop = int24(bound(drop, 12, 120));
        int24 lo = _legalLo(market + 60, market, loSeed);
        (MoveOut memory dn, MoveOut memory up) = _mirrored(frac, drop, lo);
        if (dn.reverted || up.reverted) return;
        assertLe((dn.net + up.net) / 2, 1, "M-ADV3-move: the mean over mirrored markouts must lose");
    }

    function _mirrored(uint256 frac, int24 drop, int24 lo) internal returns (MoveOut memory dn, MoveOut memory up) {
        uint256 snap = vm.snapshotState();
        int24 c = market;
        dn = _moveAttackAt(frac, drop, lo);
        vm.revertToState(snap);
        market = c;
        up = _moveAttackAt(frac, -drop, lo);
    }

    /// @notice ESCALATED, reported and not asserted (F-044, fix review S14-01): the registered row (panel D2: net ≤ 0
    ///         after a scripted adverse markout of at least 12 ticks) FAILS per draw from ≈ 70 ticks. The scan logs the
    ///         largest net over sizes 0.1–1.9% of NAV × markouts 60–110 ticks × legal ranges; s14 measured up to
    ///         ≈ +1.2e16 wei (≈ 1e-5 of NAV) at 1.9% / 110 ticks, with the reserve cap not binding. When the owner rules,
    ///         this becomes an asserted row (or the rule changes).
    function test_M_ADV3_move__escalatedPerDrawScanReported() public {
        int24[4] memory ds = [int24(60), 70, 90, 110];
        uint256[3] memory fr = [uint256(1e15), 1e16, 1.9e16];
        int256 worst = type(int256).min;
        for (uint256 f; f < 3; ++f) {
            for (uint256 j; j < 4; ++j) {
                for (uint256 k; k < 12; ++k) {
                    uint256 snap = vm.snapshotState();
                    int24 c = market;
                    MoveOut memory dn = _moveAttackAt(fr[f], ds[j], _legalLo(c + 60, c, k * 17));
                    vm.revertToState(snap);
                    market = c;
                    if (!dn.reverted && dn.net > worst) worst = dn.net;
                }
            }
        }
        console.log("M-ADV3-move per-draw scan: max net (wei)", worst);
    }

    /// @notice Row 1, pinned: a move just under the per-action cap (1.9% of NAV) and a 12-tick adverse markout acts,
    ///         is charged and loses; at 2.5% it reverts `SwapCap`.
    function test_M_ADV3_move__oneMovePinned() public {
        uint256 snap = vm.snapshotState();
        MoveOut memory o = _moveAttack(1.9e16, 12);
        assertFalse(o.reverted, "the move acts");
        assertGt(o.charge0, 0, "charged");
        assertLe(o.net, 1, "the attack must lose money");
        console.log("M-ADV3-move 1.9% / 12 ticks: net (wei)", o.net);
        console.log("  depositors' loss, reserve", uint256(o.depositorLoss > 0 ? o.depositorLoss : int256(0)), o.reserve);
        vm.revertToState(snap);
        o = _moveAttack(2.5e16, 12);
        assertTrue(o.reverted, "past the cap");
        assertEq(o.err, TouchstoneVault.SwapCap.selector, "SwapCap");
    }

    /// @notice Row 2: repeated moves across cooldowns within one epoch hit `SwapCap` once the epoch's moves would pass
    ///         `MAX_SWAP_EPOCH`.
    function test_M_ADV3_move__repeatedMovesHitTheEpochCap() public {
        (uint256 moved, uint256 n, bytes4 err) = _repeat(1.6e16, 6);
        assertEq(err, TouchstoneVault.SwapCap.selector, "the epoch cap stops the moves");
        assertLe(moved, tier.MAX_SWAP_EPOCH, "moved within the epoch cap");
        assertEq(n, 3, "three moves of 1.6% fit 5%");
    }

    /// @dev Up to `max` placements, each moving spot through JIT worth `frac` of NAV, one cooldown apart, in one epoch;
    ///      spot is unwound to the market and the markout settled between them. Returns the epoch counter, the moves
    ///      that acted, and the first revert.
    function _repeat(uint256 frac, uint256 max) internal returns (uint256 moved, uint256 n, bytes4 err) {
        // start at an epoch boundary so every move falls in one epoch
        uint256 ep = tier.EPOCH;
        advance(ep - (vm.getBlockNumber() % ep) + 1);
        v.settle();
        (uint32 e0,,,,) = v.counters();
        int24 c = market;
        for (uint256 i; i < max; ++i) {
            uint128 J = _jitFor(frac);
            syb.lp(c, c + 60, int256(uint256(J)));
            (bool rev, bytes4 e) = _try(_callPlace(_midLo(c + 60, c), _none(), _sqrt(c + 60)));
            syb.lp(c, c + 60, -int256(uint256(J)));
            syb.pushTo(_sqrt(c));
            if (rev) return (moved, n, e);
            ++n;
            (uint32 e1, uint64 f,,,) = v.counters();
            assertEq(e1, e0, "one epoch");
            moved = f;
            _settleDue();
        }
    }
}

/// @notice The positive controls of M-ADV3-move on `VaultHarness` (the production code plus test seams; the vault's
///         runtime is unchanged): MU-239/240 (the move held to neither swap cap) and MU-241 (the move not counted).
contract MADV3MoveCtlTest is MoveRows {
    function _vaultCode() internal pure override returns (bytes memory) {
        return type(VaultHarness).creationCode;
    }

    /// @notice Control for row 1 (MU-239/240 applied): a move worth 30% of NAV and a 60-tick adverse markout: the
    ///         depositors lose more than the reserve and the coalition profits.
    function test_M_ADV3_move__ctlUncappedMoveProfits() public {
        VaultHarness(vault).setControls(false, address(0), true, false);
        // the vault then holds mostly token0: the range sits with spot near its lower edge
        MoveOut memory o = _moveAttackAt(3e17, 60, market - 200);
        console.log("control: reserve", o.reserve);
        assertFalse(o.reverted, "the control acts");
        assertGt(o.depositorLoss, int256(o.reserve), "control: the depositors lose more than the reserve");
        assertGt(o.net, 0, "control: the coalition profits");
        console.log("M-ADV3-move control: net, depositors' loss (wei)", uint256(o.net), uint256(o.depositorLoss));
    }

    /// @notice Control for row 2 (MU-241 applied): the moves are not counted, so they continue past the epoch cap.
    function test_M_ADV3_move__ctlUncountedMovesPassTheEpochCap() public {
        VaultHarness(vault).setControls(false, address(0), false, true);
        uint256 n;
        uint256 ep = tier.EPOCH;
        advance(ep - (vm.getBlockNumber() % ep) + 1);
        v.settle();
        int24 c = market;
        uint256 total;
        for (uint256 i; i < 6; ++i) {
            uint128 J = _jitFor(1.6e16);
            syb.lp(c, c + 60, int256(uint256(J)));
            (bool rev,) = _try(_callPlace(_midLo(c + 60, c), _none(), _sqrt(c + 60)));
            syb.lp(c, c + 60, -int256(uint256(J)));
            syb.pushTo(_sqrt(c));
            if (rev) break;
            ++n;
            total += 1.6e16;
            _settleDue();
        }
        assertGt(total, tier.MAX_SWAP_EPOCH, "control: the epoch's moves pass MAX_SWAP_EPOCH");
        assertGe(n, 4, "control: more moves than the cap allows");
    }
}
