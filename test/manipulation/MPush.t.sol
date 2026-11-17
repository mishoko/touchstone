// SPDX-License-Identifier: MIT
pragma solidity 0.8.26;

import {console} from "forge-std/console.sol";
import {CellBase} from "./CellBase.sol";
import {MockRefPool} from "../utils/MockRefPool.sol";
import {TouchstoneVault} from "../../src/TouchstoneVault.sol";
import {ITouchstoneHook} from "../../src/interfaces/ITouchstoneHook.sol";
import {PreSwap} from "../../src/types/PreSwap.sol";

/// @notice M-1, M-2 and M-2b (spec §5.1 T1, T2): pushing the pool's price at the valuation point. M-1 pushes in the
///         action's own block; M-2 pushes in an earlier block and holds the price across block boundaries (the hook's
///         snapshots record the pushed price); M-2b does it in a thin pool (no third-party depth) with dust swaps that
///         fill the snapshot ring. The actions price at the worse of spot and the deep reference's TWAP, which the pool's
///         price does not move, under the fence; an unreadable reference locks the curator out. Positive control: a
///         reference that follows the pool's price (the thin-pool premise) makes each push profit.
contract MPushTest is CellBase {
    /// @dev The action of a row: 0 a placement with no swap, 1 a placement with a 1% pre-swap, 2 a 1% `rebalanceSwap`.
    function _act(uint256 kind, int24 s, bool z) internal returns (Out memory o) {
        if (kind == 2) return _attackSwap(0, 0, 0, 0, z, _amt(z, 1e16));
        PreSwap memory ps = kind == 1 ? PreSwap(true, z, _amt(z, 1e16)) : _none();
        return _attackPlace(0, _midLo(s, market), ps, 0);
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

    /// @notice The grid: both exit states × pushes 5, 10, 20, 40, 60 × both sides × the three action kinds; the push in
    ///         the same block, then the action, then the unwind. Every row loses; the rows act.
    function test_M_1__sameBlockPushAtTheValuationPointLoses() public {
        int24[5] memory ds = [int24(5), 10, 20, 40, 60];
        uint256 acted;
        for (uint256 st = 1; st < 3; ++st) {
            uint256 snap = vm.snapshotState();
            _pickState(st);
            uint256 snap2 = vm.snapshotState();
            for (uint256 i; i < 5; ++i) {
                for (uint256 sg; sg < 2; ++sg) {
                    for (uint256 kind; kind < 3; ++kind) {
                        int24 s = market + (sg == 0 ? ds[i] : -ds[i]);
                        syb.pushTo(_sqrt(s));
                        Out memory o = _act(kind, s, _side(st));
                        _assertLoses(o, "M-1");
                        if (!o.reverted && o.charge0 > 0) ++acted;
                        vm.revertToState(snap2);
                    }
                }
            }
            vm.revertToState(snap);
        }
        console.log("M-1 rows acted and charged", acted, "of 60");
        assertGe(acted, 40, "non-vacuity");
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
        syb.pushTo(_sqrt(s));
        _hold(n);
        Out memory o = _act(kind % 3, s, _side(1 + st % 2));
        if (d > 60 || d < -60) {
            assertTrue(o.reverted, "past the fence");
            assertEq(o.err, TouchstoneVault.Fence.selector, "Fence");
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
        syb.pushTo(_sqrt(s));
        _hold(10);
        Out memory o = _act(kind % 3, s, _side(1 + st % 2));
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
            v.withdraw(1e18, 0, 0, alice);
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
