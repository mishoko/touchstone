// SPDX-License-Identifier: MIT
pragma solidity 0.8.26;

import {TickMath} from "@uniswap/v4-core/src/libraries/TickMath.sol";
import {Tier} from "../../src/types/Tier.sol";

/// @notice A line-by-line port of the reference model's hook (`model/reference.py`, class `Hook`) for differential
///         fuzzing. Unpacked state, no gas concerns, readable control flow; the reference read is a scripted value.
///         A revert in `armAtSeed` rolls the whole call back, as in the model's `Revert`.
contract HookModel {
    uint256 internal constant N_SNAP = 32;
    uint256 internal constant K_MED = 9;

    uint256 public GRID;
    uint256 public GRID_SEC;
    uint256 public EPOCH;
    uint256 public CURE;
    uint256 public RESET_RUN;

    // the scripted reference: every read returns refNow, or fails
    int24 public refNow;
    bool public refFails;

    bool public armed;
    uint256 public lastSnap;
    uint256 public tauV;
    uint256 public tauB;
    uint256 public outBlocks;
    int24 public refTick;
    uint256 public refGrid;
    uint256 public refCachedAt;
    uint256 public resetStreak;
    bool public gapSinceVerify;
    bool public emptyRange = true;
    int24 public vaultLo;
    int24 public vaultHi;
    uint256 public breachSeq;
    uint256 public pendingBreaches;
    uint256 public lastPendingBlock;
    uint256 public tauVAtLastPending;
    int24 public epochStartRef;
    uint256 public epochStartRefEpoch;
    bool public epochStartRefSet;
    int24 public witLo;
    int24 public witHi;
    int24[] public ringTicks;
    int24 public med9;

    struct FrozenRec {
        int24 med9;
        uint256 tauV;
        uint256 tauB;
        bool exists;
    }

    mapping(uint256 => FrozenRec) public frozen;
    uint256[] internal _written;

    constructor(Tier memory t) {
        (GRID, GRID_SEC, EPOCH, CURE, RESET_RUN) = (t.GRID, t.GRID_SEC, t.EPOCH, t.CURE_BLOCKS, t.RESET_RUN);
    }

    function setRef(int24 t, bool fails) external {
        (refNow, refFails) = (t, fails);
    }

    function setWitness(int24 lo, int24 hi) external {
        (witLo, witHi) = (lo, hi);
    }

    function written() external view returns (uint256[] memory) {
        return _written;
    }

    function _sat(uint256 x, uint256 cap) internal pure returns (uint256) {
        return x > cap ? cap : x;
    }

    function _in(int24 lo, int24 hi, int24 t) internal pure returns (bool) {
        return lo <= t && t < hi;
    }

    function _read() internal view returns (bool, int24) {
        if (refFails) return (false, 0);
        int24 t = refNow;
        if (t < TickMath.MIN_TICK) t = TickMath.MIN_TICK;
        if (t > TickMath.MAX_TICK) t = TickMath.MAX_TICK;
        return (true, t);
    }

    function _writeCache(int24 t, uint256 k, uint256 now_) internal {
        (refTick, refGrid, refCachedAt) = (t, k, now_);
        uint256 e = now_ / EPOCH;
        if (!epochStartRefSet || epochStartRefEpoch != e) {
            (epochStartRef, epochStartRefEpoch, epochStartRefSet) = (t, e, true);
        }
    }

    function _commit(bool ok, int24 t, uint256 k, uint256 now_) internal {
        if (!ok) return;
        if (_in(vaultLo, vaultHi, t)) {
            resetStreak = refGrid + 1 == k ? _min(resetStreak + 1, RESET_RUN) : 1;
        } else {
            resetStreak = 0;
        }
        _writeCache(t, k, now_);
        if (resetStreak >= RESET_RUN) {
            outBlocks = 0;
            gapSinceVerify = false;
            resetStreak = 0;
        }
    }

    function _latch(uint256 old, uint256 new_) internal {
        uint256 j = old / CURE + 1;
        uint256 n;
        while (j * CURE <= new_ && n < 3) {
            breachSeq = _sat(breachSeq + 1, type(uint32).max);
            pendingBreaches = _sat(pendingBreaches + 1, type(uint32).max);
            lastPendingBlock = lastSnap + (j * CURE - old);
            tauVAtLastPending = tauV;
            ++j;
            ++n;
        }
    }

    function _insert(int24 tick) internal {
        ringTicks.push(tick);
        uint256 len = ringTicks.length;
        uint256 from = len > K_MED ? len - K_MED : 0;
        int24[] memory a = new int24[](len - from);
        for (uint256 i = from; i < len; ++i) {
            a[i - from] = ringTicks[i];
        }
        for (uint256 i; i < a.length; ++i) {
            for (uint256 j = i + 1; j < a.length; ++j) {
                if (a[j] < a[i]) (a[i], a[j]) = (a[j], a[i]);
            }
        }
        med9 = a[(a.length - 1) / 2];
    }

    function snapshot(uint160 sqrtPrice, int24 tick) public {
        delete _written;
        uint256 now_ = block.number;
        uint256 ts = block.timestamp;
        if (now_ == lastSnap && ringTicks.length > 0) return;
        if (!armed) {
            _insert(tick);
            lastSnap = now_;
            return;
        }
        uint256 G = GRID;
        uint256 k = ts / GRID_SEC;
        bool rd = k > refGrid;
        bool ok;
        int24 t;
        if (rd) (ok, t) = _read();
        int24 lo = vaultLo;
        int24 hi = vaultHi;
        uint256 last = lastSnap;
        uint256 e = _min(now_, refCachedAt + 2 * G);
        uint256 trunc = e > last ? e - last : 0;
        uint256 oldOut = outBlocks;
        uint256 tauv0 = tauV;
        uint256 taub0 = tauB;
        bool creditV = !emptyRange && TickMath.getSqrtPriceAtTick(lo) < sqrtPrice
            && sqrtPrice < TickMath.getSqrtPriceAtTick(hi) && _in(lo, hi, refTick);
        if (creditV) tauV = _sat(tauV + trunc, type(uint64).max);
        bool creditB = _in(witLo, witHi, tick);
        if (creditB) tauB = _sat(tauB + trunc, type(uint64).max);
        if (!_in(lo, hi, refTick)) outBlocks = _sat(outBlocks + trunc, type(uint64).max);
        uint256 newOut = outBlocks;
        if (now_ > refCachedAt + 2 * G) gapSinceVerify = true;
        uint256 bmax = (now_ / G) * G;
        uint256 bmin = (last / G) * G + G;
        if (bmax >= 7 * G && bmax - 7 * G > bmin) bmin = bmax - 7 * G;
        for (uint256 B = bmin; B <= bmax; B += G) {
            if (B <= last) continue;
            uint256 eb = _min(B, refCachedAt + 2 * G);
            uint256 cr = eb > last ? eb - last : 0;
            cr = _min(cr, trunc);
            frozen[B / G] =
                FrozenRec(med9, creditV ? _sat(tauv0 + cr, type(uint64).max) : tauv0, creditB ? taub0 + cr : taub0, true);
            _written.push(B / G);
        }
        _insert(tick);
        if (rd) _commit(ok, t, k, now_);
        _latch(oldOut, newOut);
        lastSnap = now_;
    }

    function refresh() public {
        uint256 k = block.timestamp / GRID_SEC;
        if (k > refGrid) {
            (bool ok, int24 t) = _read();
            _commit(ok, t, k, block.number);
        }
    }

    function refreshRef(uint160 sqrtPrice, int24 tick) external {
        snapshot(sqrtPrice, tick);
        refresh();
    }

    function armAtSeed() external {
        delete _written;
        armed = true;
        lastSnap = block.number;
        refresh();
        require(refCachedAt == block.number, "RefUnreadable");
    }

    function setVaultRange(int24 lo, int24 hi, int24 liveTick) external {
        delete _written;
        (vaultLo, vaultHi) = (lo, hi);
        emptyRange = false;
        _writeCache(liveTick, block.timestamp / GRID_SEC, block.number);
        gapSinceVerify = false;
        resetStreak = 0;
        outBlocks = 0;
    }

    function ackBreaches(uint256 n) external {
        delete _written;
        pendingBreaches = n >= pendingBreaches ? 0 : pendingBreaches - n;
    }

    function _min(uint256 a, uint256 b) internal pure returns (uint256) {
        return a < b ? a : b;
    }
}
