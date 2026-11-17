// SPDX-License-Identifier: MIT
pragma solidity 0.8.26;

import {console} from "forge-std/console.sol";
import {CellBase} from "./CellBase.sol";
import {ITouchstoneHook} from "../../src/interfaces/ITouchstoneHook.sol";
import {Obs} from "../../src/types/Obs.sol";

/// @notice M-N3 (spec §5.1 N3, §3.8): forging the disclosure ledger by oscillation. The allocation ledger is computed
///         off-chain from the on-chain inputs this build emits: each `Checkpoint`'s τ_v and τ_b (the exposure terms,
///         Σ L·Δτ), the hook's frozen grid medians `med9` (the endpoint LVR term) and the vault's (L, lo, hi). A Sybil
///         oscillates the pool price across the vault's upper edge (50 ticks out and back), at the tier's real m,
///         against the no-attack world over the same 4 grids:
///         - Lane 1, inside each block (out and back in one block, every block): the ledger's inputs are identical
///           to the no-attack world (each block's snapshot reads the price before the round trip), and the
///           coalition loses the fees.
///         - Lane 2, across blocks (out in one block, back in the next): the only input that moves is τ_v, and only
///           down (a forged ledger can make the curator look less in range, never more); the coalition loses.
///         The forgery cost (the coalition's loss per round trip and per block of τ_v moved) is reported.
contract MN3Test is CellBase {
    struct Ledger {
        uint64 tauV;
        uint64 tauB;
        int24 med9;
        int256 wealth;
    }

    function _cellM(uint64 m) internal pure override returns (uint64) {
        return m;
    }

    function _run(uint256 lane, uint256 blocks) internal returns (Ledger memory g) {
        (, int24 hi) = (_h().lo, _h().hi);
        (uint160 start,) = _spot();
        uint160 out = _sqrt(hi + 50);
        int256 w0 = _wealth();
        Obs memory o0 = ITouchstoneHook(hook).obs();
        for (uint256 i; i < blocks; ++i) {
            advance(1);
            ITouchstoneHook(hook).refreshRef();
            if (lane == 1) {
                syb.pushTo(out);
                syb.pushTo(start);
            } else if (lane == 2) {
                syb.pushTo(i % 2 == 0 ? out : start);
            }
        }
        if (lane == 2) syb.pushTo(start);
        advance(1);
        v.settle();
        Obs memory o1 = ITouchstoneHook(hook).obs();
        g = Ledger(o1.tauV - o0.tauV, o1.tauB - o0.tauB, o1.med9, _wealth() - w0);
    }

    function test_M_N3__oscillationCannotForgeTheLedgerAndLoses() public {
        uint256 n = 4 * tier.GRID;
        // start between two ticks: a swap that ends exactly on a tick boundary from above records the tick below it
        // (v4), which would move the recorded tick by one at an unchanged price
        _thirdPartyPush(uint160((uint256(_sqrt(0)) + _sqrt(1)) / 2));
        advance(1);
        v.settle();
        uint256 s = vm.snapshotState();
        Ledger memory base = _run(0, n);
        vm.revertToState(s);
        Ledger memory l1 = _run(1, n);
        vm.revertToState(s);
        Ledger memory l2 = _run(2, n);
        vm.revertToState(s);
        assertGt(base.tauV, 0, "non-vacuous: the no-attack world is in range");
        // lane 1: identical inputs
        assertEq(l1.tauV, base.tauV, "lane 1: tau_v");
        assertEq(l1.tauB, base.tauB, "lane 1: tau_b");
        assertEq(l1.med9, base.med9, "lane 1: the endpoint median");
        int256 net1 = l1.wealth - base.wealth;
        assertLt(net1, 0, "lane 1 loses");
        // lane 2: tau_v only down, tau_b and the endpoint unchanged
        assertLe(l2.tauV, base.tauV, "lane 2: tau_v never up");
        assertGt(base.tauV - l2.tauV, 0, "lane 2 moves tau_v (non-vacuous)");
        assertEq(l2.tauB, base.tauB, "lane 2: tau_b");
        int256 net2 = l2.wealth - base.wealth;
        assertLt(net2, 0, "lane 2 loses");
        console.log("M-N3 forgery cost: lane 1 loss per round trip (token1 units)", uint256(-net1) / n);
        console.log("M-N3 forgery cost: lane 2 loss per block of tau_v moved", uint256(-net2) / (base.tauV - l2.tauV));
    }
}
