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
        /// the vault's real loss against the counterfactual at the end (NAV at the market, before any burn matters):
        /// the test's own oracle for whether the settlement is capped (`vaultLoss > charge0 + reserve`)
        int256 vaultLoss;
    }

    /// @dev Where the arbitrage after the action stops, in ticks from the market (0: at the market), in both worlds.
    int24 internal stopOff;
    /// @dev The counterfactual's own stop when it differs (`type(int24).min`: the same as `stopOff`).
    int24 internal baseStop = type(int24).min;

    function _aliceValue() internal view returns (int256) {
        return int256(FullMath.mulDiv(v.balanceOf(alice), _navReal(), v.totalSupply()));
    }

    /// @notice The path common to the attack and its counterfactual: the market drops `drop` ticks, an arbitrageur
    ///         (the Sybil) moves the pool's spot there, the markout settles.
    function _afterPath(int24 drop) internal returns (uint256 extra) {
        int24 c = market;
        market = c - drop;
        syb.pushTo(_sqrt(market + stopOff));
        extra = _settleDueExtra();
        v.settle();
    }

    /// @dev The counterfactual: no JIT and no action over the same market path (the charge's own counterfactual,
    ///      NAV_before); the state is restored after. Returns the Sybil's value, the curator's shares, the depositor's
    ///      value and the vault's real NAV at its end.
    function _baseline(int24 drop) internal returns (int256 sB, uint256 cB, int256 aB, int256 nB) {
        uint256 snap = vm.snapshotState();
        int24 c = market;
        int24 so = stopOff;
        if (baseStop != type(int24).min) stopOff = baseStop;
        _dropAfter(drop);
        _afterPath(drop);
        stopOff = so;
        (sB, cB, aB, nB) = (_sybilValue(), _curatorShares(), _aliceValue(), int256(_navReal()));
        vm.revertToState(snap);
        market = c;
    }

    function _moveAttack(uint256 frac, int24 drop) internal returns (MoveOut memory o) {
        return _moveAttackAt(frac, drop, _midLo(market + 60, market));
    }

    /// @dev The counterfactual's end values (`_baseline`), computed once per (markout, stop) by the scans.
    struct Base {
        int256 sB;
        uint256 cB;
        int256 aB;
        int256 nB;
    }

    /// @dev The counterfactual for `drop` from the current state (settled first, as every row is), with `stopOff`
    ///      (and `baseStop`) as set; the state is unchanged after.
    function _baseFor(int24 drop) internal returns (Base memory b) {
        uint256 snap = vm.snapshotState();
        int24 c = market;
        v.settle();
        (b.sB, b.cB, b.aB, b.nB) = _baseline(drop);
        vm.revertToState(snap);
        market = c;
    }

    function _moveAttackAt(uint256 frac, int24 drop, int24 lo) internal returns (MoveOut memory o) {
        v.settle();
        (int256 sB, uint256 cB, int256 aB, int256 nB) = _baseline(drop);
        return _moveAttackWith(frac, drop, lo, Base(sB, cB, aB, nB));
    }

    /// @dev The attack against a counterfactual already computed for the same start state, markout and stop.
    function _moveAttackWith(uint256 frac, int24 drop, int24 lo, Base memory base) internal returns (MoveOut memory o) {
        v.settle();
        int24 c = market;
        uint128 J = _jitFor(frac);
        (int256 sB, uint256 cB, int256 aB, int256 nB) = (base.sB, base.cB, base.aB, base.nB);
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
        o.vaultLoss = nB - int256(_navReal());
    }

    /// @dev The test's oracle: the settlement cannot cover the row (the reserve caps it).
    function _capped(MoveOut memory o) internal pure returns (bool) {
        return o.vaultLoss > int256(o.charge0 + o.reserve);
    }

    /// @dev Twelve legal ranges for spot `c + 60` and the reference `c`, spread evenly from one extreme to the other.
    function _rangeAt(int24 c, uint256 k) internal view returns (int24) {
        int24 loMin = _floor(c + 60) + TICK_SPACING - W;
        int24 loMax = _floor(c);
        uint256 n = uint256(int256((loMax - loMin) / TICK_SPACING));
        return loMin + int24(int256(n * k / 11)) * TICK_SPACING;
    }

    /// @dev One row from the start state, restored after.
    function _row(uint256 frac, int24 drop, uint256 k, int24 stop) internal returns (MoveOut memory o) {
        uint256 snap = vm.snapshotState();
        int24 c = market;
        stopOff = stop;
        o = _moveAttackAt(frac, drop, _rangeAt(c, k));
        vm.revertToState(snap);
        market = c;
        stopOff = 0;
    }

    /// @dev The counterfactual for a row with the attack stopping at `stop` and the counterfactual at `bStop`.
    function _baseAt(int24 drop, int24 stop, int24 bStop) internal returns (Base memory b) {
        (stopOff, baseStop) = (stop, bStop);
        b = _baseFor(drop);
        (stopOff, baseStop) = (int24(0), type(int24).min);
    }

    /// @dev `_row` against a precomputed counterfactual (`_baseAt` with the same markout and stops).
    function _rowB(uint256 frac, int24 drop, uint256 k, int24 stop, Base memory b) internal returns (MoveOut memory o) {
        uint256 snap = vm.snapshotState();
        int24 c = market;
        stopOff = stop;
        o = _moveAttackWith(frac, drop, _rangeAt(c, k), b);
        vm.revertToState(snap);
        market = c;
        stopOff = 0;
    }
}

contract MADV3MoveTest is MoveRows {
    /// @notice Row 1 at a 12-tick markout (fuzz over sizes up to just past the per-action cap and any legal range; the
    ///         full per-draw rule is the scan below; the cap itself is pinned in `oneMovePinned` and U-guard): a guard
    ///         reverts (`SwapCap`; `DeltaMin` for a range the post-move inventory cannot fill), or the coalition loses and
    ///         the depositors lose at most the reserve. At 12 ticks the charge alone decides: the caps-off control at 12
    ///         ticks still loses (fix review S14-04).
    /// forge-config: default.fuzz.runs = 64
    function test_M_ADV3_move__oneMoveRevertsSwapCapOrLoses(uint256 frac, uint256 loSeed) public {
        frac = bound(frac, 1e15, 2.5e16);
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

    int24[12] internal MKS = [int24(12), 30, 60, 90, 120, 150, 180, 240, 300, 400, 500, 600];
    uint256[3] internal SIZES = [uint256(1e15), 1e16, 1.9e16];

    /// @notice D2 per draw (owner ruling s15: bounded to the uncapped region; spec §9.1). Sizes 0.1, 1 and 1.9% of NAV ×
    ///         both directions × twelve legal ranges × markouts from 12 ticks to twice the (size, direction)'s break-even
    ///         (or 600) × the arbitrage's stop on each settlement leg: at the market and at the leg on the arbitrage's side
    ///         (between the post-action spot and the market) in both worlds; at the leg past the market (an overshoot)
    ///         in the attack, with the counterfactual stopping at the market, its best response. On every row the
    ///         oracle finds uncapped, the coalition nets ≤ 1 wei (the defence's check; the depositors' loss ≤ the reserve
    ///         also holds, but follows from the oracle's filter); a guard is the only revert. Reported: each break-even
    ///         and the capped rows' worst net (the disclosed tail). The counterfactual is computed once per (markout,
    ///         stop) and shared by the rows (it does not depend on the size or the range).
    function test_M_ADV3_move__perDrawUncappedOnLegLoses() public {
        uint256 uncapped;
        int256 worstCapped = type(int256).min;
        int256 worstUncapped = type(int256).min;
        int24[3][2] memory be;
        for (uint256 j; j < 12; ++j) {
            for (uint256 d; d < 2; ++d) {
                int24 drop = d == 0 ? MKS[j] : -MKS[j];
                int24 near = drop > 0 ? tier.FEE_TICKS + 1 : -tier.FEE_TICKS;
                int24 over = drop > 0 ? -tier.FEE_TICKS : tier.FEE_TICKS + 1;
                for (uint256 st; st < 3; ++st) {
                    // st 0: the market; 1: the leg on the arbitrage's side; 2: the overshoot leg (baseline at mk)
                    int24 stop = st == 0 ? int24(0) : (st == 1 ? near : over);
                    Base memory b = _baseAt(drop, stop, st == 2 ? int24(0) : type(int24).min);
                    for (uint256 f; f < 3; ++f) {
                        if (be[d][f] != 0 && MKS[j] > 2 * be[d][f]) continue;
                        for (uint256 k; k < 12; ++k) {
                            MoveOut memory o = _rowB(SIZES[f], drop, k, stop, b);
                            if (o.reverted) {
                                _checkMove(o);
                                continue;
                            }
                            if (_capped(o)) {
                                if (be[d][f] == 0) be[d][f] = MKS[j];
                                if (o.net > worstCapped) worstCapped = o.net;
                                continue;
                            }
                            ++uncapped;
                            if (o.net > worstUncapped) worstUncapped = o.net;
                            assertLe(o.net, 1, "M-ADV3-move per draw: the attack must lose money (uncapped, on a leg)");
                            assertLe(o.depositorLoss, int256(o.reserve), "the depositors lose at most the reserve");
                        }
                    }
                }
            }
        }
        for (uint256 f; f < 3; ++f) {
            console.log(
                string.concat(
                    "M-ADV3-move size ", vm.toString(SIZES[f]), ": break-even markout, adverse / favourable (0: none to 600) ",
                    vm.toString(int256(be[0][f])), " / ", vm.toString(int256(be[1][f]))
                )
            );
        }
        console.log("M-ADV3-move uncapped on-leg rows", uncapped);
        console.log("  worst uncapped net (wei)", worstUncapped);
        console.log("  worst capped net (wei; the disclosed tail)", worstCapped);
        assertGe(uncapped, 1000, "non-vacuity: uncapped rows");
    }

    /// @dev One reported stop class: rows over the first six markouts (one direction or both), sizes, every third range;
    ///      a guard is the only revert; returns the uncapped rows' count and worst net (split by the arbitrage's side when
    ///      `split`: [0] the arbitrage's side, [1] past the market).
    function _reported(int24 stopAdverse, int24 stopFavourable, int24 bStop, bool both)
        internal
        returns (uint256 n, int256[2] memory worst)
    {
        worst = [type(int256).min, type(int256).min];
        for (uint256 j; j < 6; ++j) {
            for (uint256 d; d < (both ? 2 : 1); ++d) {
                int24 drop = d == 0 ? MKS[j] : -MKS[j];
                int24 stop = d == 0 ? stopAdverse : stopFavourable;
                Base memory b = _baseAt(drop, stop, bStop);
                for (uint256 f; f < 3; ++f) {
                    for (uint256 k; k < 12; k += 3) {
                        MoveOut memory o = _rowB(SIZES[f], drop, k, stop, b);
                        if (o.reverted) {
                            _checkMove(o);
                            continue;
                        }
                        if (_capped(o)) continue;
                        ++n;
                        assertLe(o.depositorLoss, int256(o.reserve), "the depositors lose at most the reserve");
                        // the arbitrage's side: above the market for an adverse (falling) markout, below for a rise
                        uint256 w = (drop > 0) == (stop > 0) ? 0 : 1;
                        if (o.net > worst[w]) worst[w] = o.net;
                    }
                }
            }
        }
    }

    /// @notice Reported (not claimed), with a ratchet on the measured worst: both worlds' arbitrage overshooting the
    ///         market to the far leg, which costs the arbitrageur in both. s15 measured ≤ ≈ 9.1e14 wei, all from the
    ///         counterfactual's own overshoot (with it stopping at the market every row loses: the scan above).
    function test_M_ADV3_move__bothWorldsOvershootReported() public {
        (uint256 n, int256[2] memory w) = _reported(-tier.FEE_TICKS, tier.FEE_TICKS + 1, type(int24).min, true);
        console.log("M-ADV3-move both worlds overshooting: uncapped rows", n);
        console.logInt(w[1]);
        assertGe(n, 50, "non-vacuity");
        assertLe(w[1], 1e15, "ratchet: the measured worst (s15)");
    }

    /// @notice Reported, with a ratchet: the arbitrage stopping inside the fee band off a leg (`mk ± 3`) in both worlds.
    ///         s15: on the arbitrage's side every row loses (worst ≈ −2.5e14, asserted ≤ 1 wei); past the market ≤ ≈ 3.7e14.
    function test_M_ADV3_move__offLegStopsReported() public {
        (uint256 n1, int256[2] memory a) = _reported(3, -3, type(int24).min, true);
        (uint256 n2, int256[2] memory b) = _reported(-3, 3, type(int24).min, true);
        console.log("M-ADV3-move off-leg stops (mk +/- 3): uncapped rows", n1 + n2);
        console.log("  worst net, the arbitrage's side / past the market (wei):");
        console.logInt(a[0]);
        console.logInt(b[1]);
        assertGe(n1 + n2, 100, "non-vacuity");
        assertLe(a[0], 1, "the arbitrage's side, off a leg, loses");
        assertLe(b[1], 4e14, "ratchet: past the market (s15)");
    }

    /// @notice Reported, with a ratchet: a protocol fee of 1,000 pips switched on, so a rational arbitrage stops ≈ 15
    ///         ticks short of the market on its side (disclosed in spec §5.2). s15 measured ≤ ≈ 4.2e15 wei.
    function test_M_ADV3_move__protocolFeeStopsReported() public {
        manager.setProtocolFeeController(address(this));
        manager.setProtocolFee(key, uint24(1000) | (uint24(1000) << 12));
        (uint256 n, int256[2] memory w) = _reported(15, -15, type(int24).min, true);
        console.log("M-ADV3-move protocol-fee stops: uncapped rows", n);
        console.logInt(w[0]);
        assertGe(n, 50, "non-vacuity");
        assertLe(w[0], 5e15, "ratchet: the measured worst (s15)");
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

    /// @notice Control for the per-draw row (F-044): the s14 settlement (holdings-only outer legs, the immediate charge
    ///         netted at face value) on the uncapped on-leg rows at 1.9% of NAV and a 110-tick adverse markout: the
    ///         coalition profits on at least one range (s14 measured +1.23e16 wei), so the fee-aware settlement is what
    ///         makes the per-draw row lose.
    function test_M_ADV3_move__ctlS14SettlementProfitsPerDraw() public {
        VaultHarness(vault).setSettleS14(true);
        int256 best = type(int256).min;
        for (uint256 k; k < 12; ++k) {
            MoveOut memory o = _row(1.9e16, 110, k, 0);
            if (o.reverted || _capped(o)) continue;
            if (o.net > best) best = o.net;
        }
        console.log("M-ADV3-move control (s14 settlement): best uncapped net (wei)", best);
        assertGt(best, 0, "control: the s14 settlement lets the coalition profit");
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
