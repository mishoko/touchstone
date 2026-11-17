// SPDX-License-Identifier: MIT
pragma solidity 0.8.26;

import {console} from "forge-std/console.sol";
import {CellBase} from "./CellBase.sol";
import {MockRefPool} from "../utils/MockRefPool.sol";
import {TouchstoneVault} from "../../src/TouchstoneVault.sol";
import {ITouchstoneHook} from "../../src/interfaces/ITouchstoneHook.sol";
import {PreSwap} from "../../src/types/PreSwap.sol";
import {IVaultEntries} from "../../src/interfaces/ITouchstoneVaultExtension.sol";
import {VaultCommon} from "../../src/TouchstoneVaultBase.sol";

/// @notice M-1, M-2 and M-2b (spec §5.1 T1, T2): pushing the pool's price at the valuation point. M-1 pushes in the
///         action's own block; M-2 pushes in an earlier block and holds the price across block boundaries (the hook's
///         snapshots record the pushed price); M-2b does it in a thin pool (no third-party depth) with dust swaps that
///         fill the snapshot ring. The actions price at the worse of spot and the deep reference's TWAP, which the pool's
///         price does not move, under the fence; an unreadable reference locks the curator out. Positive control: a
///         reference that follows the pool's price (the thin-pool premise) makes each push profit.
contract MPushTest is CellBase {
    /// @dev The third-party depth the swap rows add (× the vault's L, on `[market − 3000, market + 3000)`): in an exit
    ///      state the vault's own position is out of range, so without depth its swap would fill nothing (fix review
    ///      S14-02).
    uint256 internal constant SWAP_DEPTH = 0.3e18;

    /// @dev The action of a row: 0 a placement with no swap, 1 a placement with a 1% pre-swap, 2 a 1% `rebalanceSwap`;
    ///      `push` the Sybil's push target inside the measure (0: none, the push already happened in earlier blocks).
    function _act(uint256 kind, int24 s, bool z, uint160 push) internal returns (Out memory o) {
        if (kind == 2) return _attackSwap(push, 0, 0, 0, z, _amt(z, 1e16));
        PreSwap memory ps = kind == 1 ? PreSwap(true, z, _amt(z, 1e16)) : _none();
        return _attackPlace(push, _midLo(s, market), ps, 0);
    }

    /// @dev A row's acted and swapped counters, per action kind.
    function _count(Out memory o, uint256 kind, uint256[3] memory acted, uint256[3] memory swapped) internal pure {
        if (o.reverted || o.charge0 == 0) return;
        ++acted[kind];
        if (o.swapped > 0) ++swapped[kind];
    }

    function _amt(bool z, uint256 frac) internal view returns (uint128) {
        uint256 v1 = _nav(_sqrt(market)) * frac / WAD;
        if (!z) return uint128(v1);
        uint160 sp = _sqrt(market);
        return uint128((v1 << 96) / sp * (1 << 96) / sp);
    }

    /// @dev The swap side that spends the token the exit state holds idle (state 1: token0; state 2: token1).
    function _side(uint256 st) internal pure returns (bool) {
        return st % 3 != 2;
    }

    // ------------------------------------------------------------------ M-1: the push in the action's block

    /// @notice The grid: both exit states × pushes 5, 10, 20, 40, 60 × both sides × the three action kinds; the push
    ///         (inside the measure) in the same block, then the action, then the unwind. The swap kinds run with
    ///         third-party depth 0.3× so the vault's swap fills. Every row loses; each kind acts, and the swap kinds swap.
    function test_M_1__sameBlockPushAtTheValuationPointLoses() public {
        int24[5] memory ds = [int24(5), 10, 20, 40, 60];
        uint256[3] memory acted;
        uint256[3] memory swapped;
        for (uint256 st = 1; st < 3; ++st) {
            uint256 snap = vm.snapshotState();
            _pickState(st);
            uint256 snap2 = vm.snapshotState();
            for (uint256 i; i < 5; ++i) {
                for (uint256 sg; sg < 2; ++sg) {
                    for (uint256 kind; kind < 3; ++kind) {
                        int24 s = market + (sg == 0 ? ds[i] : -ds[i]);
                        if (kind > 0) _thirdPartyDepth(SWAP_DEPTH);
                        Out memory o = _act(kind, s, _side(st), _sqrt(s));
                        _assertLoses(o, "M-1");
                        _count(o, kind, acted, swapped);
                        vm.revertToState(snap2);
                    }
                }
            }
            vm.revertToState(snap);
        }
        console.log("M-1 rows acted (no swap, pre-swap, rebalanceSwap) of 20 each", acted[0], acted[1], acted[2]);
        console.log("M-1 rows whose vault swap filled (pre-swap, rebalanceSwap)", swapped[1], swapped[2]);
        assertGe(acted[0], 14, "non-vacuity: placements act");
        assertGe(swapped[1], 14, "non-vacuity: pre-swaps fill");
        assertGe(swapped[2], 14, "non-vacuity: rebalanceSwaps fill");
    }

    /// @notice Positive control: the reference follows the push; a 60-tick push, a placement and the unwind profit.
    function test_M_1__ctlReferenceFollowingThePushProfits() public {
        _pickState(1);
        int24 s = market - 60;
        _refFollows(s);
        syb.pushTo(_sqrt(s));
        Out memory o = _attackPlace(0, _midLo(s, s), _none(), 0);
        assertFalse(o.reverted, "the control acts");
        assertGt(o.net, 0, "control: the attack profits");
    }

    // ------------------------------------------------------------------ M-2: the push held across blocks

    /// @notice Fuzz: a push of up to the fence (and past it), held for 1–60 blocks with the hook snapshotting every
    ///         block, then the action and the unwind. Past the fence the action reverts `Fence`.
    function test_M_2__pushHeldAcrossBlocksLoses(uint8 st, int24 d, uint256 n, uint8 kind) public {
        _pickState(1 + st % 2);
        d = int24(bound(d, -80, 80));
        n = bound(n, 1, 60);
        int24 s = market + d;
        if (kind % 3 > 0) _thirdPartyDepth(SWAP_DEPTH);
        // the measure opens before the push: its cost against the depth counts
        _beginMeasure();
        syb.pushTo(_sqrt(s));
        _hold(n);
        Out memory o = _act(kind % 3, s, _side(1 + st % 2), 0);
        if (!o.reverted && kind % 3 > 0) assertGt(o.swapped, 0, "non-vacuity: the vault's swap fills");
        if (d > 60 || d < -60) {
            assertTrue(o.reverted, "past the fence");
            assertEq(o.err, VaultCommon.Fence.selector, "Fence");
        }
        _assertLoses(o, "M-2");
    }

    /// @dev `n` blocks with the pool held at its pushed price; a dust swap in each block snapshots it in the hook.
    function _hold(uint256 n) internal {
        for (uint256 i; i < n; ++i) {
            advance(1);
            ITouchstoneHook(hook).refreshRef();
            (uint160 sp,) = _spot();
            syb.swapIn(true, 1, sp - 1);
            syb.pushTo(sp);
        }
    }

    /// @notice Positive control: a reference that is the pool's own TWAP follows a push held for the whole TWAP window;
    ///         the action at the pushed price and the unwind then profit.
    function test_M_2__ctlReferenceFollowingAHeldPushProfits() public {
        _pickState(1);
        int24 s = market - 60;
        syb.pushTo(_sqrt(s));
        _hold(50);
        _refFollows(s);
        Out memory o = _attackPlace(0, _midLo(s, s), _none(), 0);
        assertFalse(o.reverted, "the control acts");
        assertGt(o.net, 0, "control: the attack profits");
    }

    // ------------------------------------------------------------------ M-2b: the thin pool, dust snapshots

    /// @notice A thin pool (no third-party depth): the push costs nothing outside the vault's range, and dust swaps fill
    ///         the hook's snapshot ring with the pushed price over ten blocks. The actions still price at the deep
    ///         reference: every row loses.
    function test_M_2b__thinPoolDustSnapshotsLose(int24 d, uint8 kind, uint8 st) public {
        _pickState(1 + st % 2);
        d = int24(bound(d, -60, 60));
        int24 s = market + d;
        _beginMeasure();
        syb.pushTo(_sqrt(s));
        _hold(10);
        Out memory o;
        if (kind % 3 == 0) {
            o = _act(0, s, _side(1 + st % 2), 0);
        } else {
            // the pool stays thin: the only depth on the swap's path is the Sybil's own JIT (fix review S14-02)
            bool z = _side(1 + st % 2);
            int24 slip = tier.FEE_TICKS + tier.BAND_SWAP;
            int24 f = _floor(s);
            (jitLo, jitHi) = z ? (f - slip - TICK_SPACING, f + TICK_SPACING) : (f, f + slip + 2 * TICK_SPACING);
            jitL = _h().L == 0 ? 1e20 : _h().L;
            if (kind % 3 == 1) {
                o = _act(1, s, z, 0);
            } else {
                // `rebalanceSwap` has no JIT hook in `_attackSwap`'s argument list other than its own
                (int24 jlo, int24 jhi, uint256 J) = (jitLo, jitHi, jitL);
                jitL = 0;
                o = _attackSwap(0, jlo, jhi, J, z, _amt(z, 1e16));
            }
            if (!o.reverted) assertGt(o.swapped, 0, "non-vacuity: the vault's swap fills against the JIT");
        }
        _assertLoses(o, "M-2b");
    }

    /// @notice An unreadable reference locks the curator out: both actions revert `RefUnreadable` in every failure mode,
    ///         while a withdrawal still succeeds.
    function test_M_2b__unreadableReferenceLocksTheCuratorOut() public {
        _pickState(1);
        MockRefPool.Mode[4] memory ms =
            [MockRefPool.Mode.Old, MockRefPool.Mode.Revert, MockRefPool.Mode.Short, MockRefPool.Mode.ReturnBomb];
        for (uint256 i; i < 4; ++i) {
            refPool.setMode(ms[i]);
            advance(1);
            (bool r1, bytes4 e1) = _try(_callPlace(_midLo(market, market), _none(), 0));
            (bool r2, bytes4 e2) = _try(_callSwap(true, 1e15));
            assertTrue(r1 && r2, "the actions revert");
            assertEq(e1, ITouchstoneHook.RefUnreadable.selector, "placement: RefUnreadable");
            assertEq(e2, ITouchstoneHook.RefUnreadable.selector, "swap: RefUnreadable");
            vm.prank(alice);
            IVaultEntries(address(v)).withdraw(1e18, 0, 0, alice);
        }
    }

    /// @notice Positive control: with the reference following the pool's price, the thin-pool push profits.
    function test_M_2b__ctlReferenceFollowingTheThinPoolProfits() public {
        _pickState(2);
        int24 s = market + 60;
        syb.pushTo(_sqrt(s));
        _hold(10);
        _refFollows(s);
        Out memory o = _attackPlace(0, _midLo(s, s), _none(), 0);
        assertFalse(o.reverted, "the control acts");
        assertGt(o.net, 0, "control: the attack profits");
    }
}
