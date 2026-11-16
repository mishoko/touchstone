// SPDX-License-Identifier: MIT
pragma solidity 0.8.26;

import {Bank, Cohort, Op, RING, V} from "../types/Bank.sol";
import {Ladder} from "./Ladder.sol";

/// @title FeeBank
/// @notice The curator's bank: cohorts, vesting, forfeiture, the forfeit drip, the bond and the markout reserve.
///         A coarse-linked external library: the vault reaches it by `delegatecall` through one entry point, and the
///         library moves bookkeeping only. The vault performs every ERC-20 mint, burn and curator transfer from the
///         returned deltas. The block is always an explicit argument (`now`); the library never reads it.
/// @dev The entry point is `bankApply` (`apply` is a reserved word in Solidity). Arguments per op, `abi.encode`d:
///      Accrue (uint32 epoch, uint256 shares, bool gateOpen); Breach (uint32 count, uint40 now);
///      VestTime (uint32 nowEpoch, bool gateOpen); Drip (uint40 now); Charge (uint256 burn, uint256 burnR); Place ();
///      Retire (uint40 now); Claim (); TopUp (uint256 shares); Release (uint256 shares, uint40 now);
///      Settle (uint256 burnS, uint32 epoch, uint40 now).
///      Returns (burnShares, payShares, a, b2): the vault burns `burnShares` and pays `payShares`; `a` and `b2` carry
///      event amounts: Charge (bank burned, bond burned); Breach and Retire (forfeited, mode after); VestTime and
///      Accrue (vested, moved to maturedPending); Drip (burned, 0); Settle (released to the bond, released to the
///      cohort or the drip); Claim, TopUp, Release, Place (shares moved, 0).
///      Pay and checkpoint arithmetic saturates at the field bounds and never reverts.
library FeeBank {
    error Insolvent();
    error MarkoutPending();
    error BondLocked();

    function bankApply(Bank storage b, Op op, bytes calldata args)
        external
        returns (uint256 burnShares, uint256 payShares, uint256 a, uint256 b2)
    {
        if (op == Op.Accrue) {
            (uint32 e, uint256 shares, bool gate) = abi.decode(args, (uint32, uint256, bool));
            return _accrue(b, e, shares, gate);
        }
        if (op == Op.Breach) {
            (uint32 count, uint40 now_) = abi.decode(args, (uint32, uint40));
            return _exit(b, Ladder.EV_BREACH, count, now_);
        }
        if (op == Op.VestTime) {
            (uint32 ne, bool gate) = abi.decode(args, (uint32, bool));
            return _vest(b, ne, gate);
        }
        if (op == Op.Drip) {
            burnShares = _drip(b, abi.decode(args, (uint40)));
            return (burnShares, 0, burnShares, 0);
        }
        if (op == Op.Charge) {
            (uint256 burn, uint256 burnR) = abi.decode(args, (uint256, uint256));
            return _charge(b, burn, burnR);
        }
        if (op == Op.Place) {
            (uint8 mode, uint32 br,) = Ladder.next(b.mode, b.breaches, Ladder.EV_PLACE, 0);
            (b.mode, b.breaches) = (mode, br);
            b.placedByCurator = true;
            return (0, 0, 0, 0);
        }
        if (op == Op.Retire) {
            return _exit(b, Ladder.EV_RETIRE, 0, abi.decode(args, (uint40)));
        }
        if (op == Op.Claim) {
            payShares = b.vestedUnclaimed;
            b.vestedUnclaimed = 0;
            return (0, payShares, payShares, 0);
        }
        if (op == Op.TopUp) {
            uint256 shares = abi.decode(args, (uint256));
            b.bondShares = _sat(uint256(b.bondShares) + shares);
            return (0, 0, shares, 0);
        }
        if (op == Op.Release) {
            (uint256 shares, uint40 now_) = abi.decode(args, (uint256, uint40));
            // [MU-046] the bond is released only in EXITED and only after the release delay
            if (b.mode != Ladder.EXITED) revert Ladder.BadMode();
            // [MU-072] the delay is `releaseDelay` (V·EPOCH), not `dripBlocks`
            if (uint256(now_) < uint256(b.exitBlock) + b.releaseDelay) revert BondLocked();
            payShares = shares < b.bondShares ? shares : b.bondShares;
            b.bondShares -= uint128(payShares);
            return (0, payShares, payShares, 0);
        }
        // Op.Settle
        (uint256 burnS, uint32 ep, uint40 now2) = abi.decode(args, (uint256, uint32, uint40));
        return _settle(b, burnS, ep, now2);
    }

    // ------------------------------------------------------------------ ops

    function _accrue(Bank storage b, uint32 e, uint256 shares, bool gate)
        private
        returns (uint256, uint256, uint256 vested, uint256 moved)
    {
        if (shares == 0) return (0, 0, 0, 0);
        Cohort storage s = b.cohorts[e % RING];
        if (s.shares > 0 && s.epoch != e) {
            // [MU-056] [MU-094] an overwrite vests its occupant only while the gate is open, else it waits matured
            if (gate) {
                vested = s.shares;
                b.vestedUnclaimed = _sat(uint256(b.vestedUnclaimed) + vested);
            } else {
                moved = s.shares;
                b.maturedPending = _sat(uint256(b.maturedPending) + moved);
            }
            s.shares = 0;
        }
        s.epoch = e;
        s.shares = _sat(uint256(s.shares) + shares);
        return (0, 0, vested, moved);
    }

    function _vest(Bank storage b, uint32 ne, bool gate) private returns (uint256, uint256, uint256 vested, uint256 moved) {
        for (uint256 k; k < RING; ++k) {
            Cohort storage c = b.cohorts[k];
            if (c.shares > 0 && uint256(ne) >= uint256(c.epoch) + 1 + V) {
                if (gate) vested += c.shares;
                else moved += c.shares;
                c.shares = 0;
            }
        }
        if (gate) {
            // [MU-082] maturedPending vests with the gate open
            vested += b.maturedPending;
            b.maturedPending = 0;
            b.vestedUnclaimed = _sat(uint256(b.vestedUnclaimed) + vested);
        } else {
            b.maturedPending = _sat(uint256(b.maturedPending) + moved);
        }
        return (0, 0, vested, moved);
    }

    /// @dev Breach (ev = EV_BREACH) and Retire (ev = EV_RETIRE): burn the drip owed at `now`, then forfeit every
    ///      unvested cohort and `maturedPending` to the drip; the reserve stays encumbered and is marked to follow.
    function _exit(Bank storage b, uint8 ev, uint32 count, uint40 now_)
        private
        returns (uint256 burned, uint256, uint256 forfeited, uint256)
    {
        // Retire reverts BadMode in EXITED before any write
        (uint8 mode, uint32 br, bool exited) = Ladder.next(b.mode, b.breaches, ev, count);
        burned = _drip(b, now_);
        // [MU-047] [MU-089] [MU-095] breach and retire forfeit everything unvested, maturedPending included
        forfeited = _forfeitAll(b, now_);
        // [MU-153] the reserve is never released here: it follows at settlement
        if (b.reserveShares > 0) b.reserveForfeit = true;
        (b.mode, b.breaches) = (mode, br);
        // [MU-071] only a transition into EXITED writes exitBlock
        if (exited) {
            b.exitBlock = now_;
            b.placedByCurator = false;
        }
        return (burned, 0, forfeited, mode);
    }

    function _charge(Bank storage b, uint256 burn, uint256 burnR)
        private
        returns (uint256, uint256, uint256 bankBurn, uint256 bondBurn)
    {
        uint256 cover = _unvested(b) + b.bondShares;
        // [MU-005] the solvency gate: the charge burn and the reserve together fit the unvested pay and the bond
        if (burn > cover || burnR > cover - burn) revert Insolvent();
        (bankBurn, bondBurn) = _take(b, burn);
        if (burnR > 0) {
            if (b.reserveShares > 0) revert MarkoutPending();
            (, uint256 fromBond) = _take(b, burnR);
            b.reserveShares = _sat(burnR);
            b.reserveBond = uint128(fromBond);
            b.reserveForfeit = false;
        }
        return (burn, 0, bankBurn, bondBurn);
    }

    function _settle(Bank storage b, uint256 burnS, uint32 e, uint40 now_)
        private
        returns (uint256, uint256, uint256 relBond, uint256 relNb)
    {
        uint256 res = b.reserveShares;
        uint256 x = burnS < res ? burnS : res;
        uint256 nonbond = res - b.reserveBond;
        // [MU-154] the settlement burns the non-bond part first
        uint256 fromNb = x < nonbond ? x : nonbond;
        relBond = b.reserveBond - (x - fromNb);
        relNb = nonbond - fromNb;
        b.bondShares = _sat(uint256(b.bondShares) + relBond);
        uint256 dripped;
        if (relNb > 0) {
            if (b.reserveForfeit) {
                // as any forfeit: burn the drip owed first, then restart it with the remainder plus the release
                dripped = _drip(b, now_);
                b.forfeitPool = _sat(uint256(b.forfeitPool) + relNb);
                b.forfeitTotal = b.forfeitPool;
                b.forfeitStart = now_;
            } else {
                Cohort storage s = b.cohorts[e % RING];
                if (s.shares > 0 && s.epoch != e) {
                    b.maturedPending = _sat(uint256(b.maturedPending) + s.shares);
                    s.shares = 0;
                }
                s.epoch = e;
                s.shares = _sat(uint256(s.shares) + relNb);
            }
        }
        b.reserveShares = 0;
        b.reserveBond = 0;
        b.reserveForfeit = false;
        return (x + dripped, 0, relBond, relNb);
    }

    // ------------------------------------------------------------------ helpers

    /// @dev The drip owed at `now` by the cumulative target `min(T, T·(now − forfeitStart)/dripBlocks)`.
    function _drip(Bank storage b, uint40 now_) private returns (uint256 burn) {
        uint256 total = b.forfeitTotal;
        uint256 pool = b.forfeitPool;
        if (total == 0 || pool == 0) return 0;
        uint256 soFar;
        // [MU-031] the forfeit burns over dripBlocks, not at once
        if (b.dripBlocks == 0) {
            soFar = total;
        } else {
            uint256 el = now_ > b.forfeitStart ? now_ - b.forfeitStart : 0;
            soFar = total * el / b.dripBlocks;
            if (soFar > total) soFar = total;
        }
        // burn = clamp(soFar − (total − pool), 0, pool)
        int256 owed = int256(soFar) - (int256(total) - int256(pool));
        if (owed <= 0) return 0;
        burn = uint256(owed) > pool ? pool : uint256(owed);
        b.forfeitPool = uint128(pool - burn);
    }

    /// @dev Moves every unvested cohort and maturedPending to the forfeit pool; restarts the drip only if the new
    ///      forfeit is > 0 (the drip owed so far has already been burned by the caller).
    function _forfeitAll(Bank storage b, uint40 now_) private returns (uint256 forfeited) {
        for (uint256 k; k < RING; ++k) {
            forfeited += b.cohorts[k].shares;
            b.cohorts[k].shares = 0; // an emptied slot keeps its epoch
        }
        forfeited += b.maturedPending;
        b.maturedPending = 0;
        // [MU-084] an empty forfeit never restarts the drip
        if (forfeited > 0) {
            // [MU-032] [MU-124] forfeited shares join the remainder of the pool and burn to every holder
            b.forfeitPool = _sat(uint256(b.forfeitPool) + forfeited);
            b.forfeitTotal = b.forfeitPool;
            b.forfeitStart = now_;
        }
    }

    /// @dev Takes `need` shares from the youngest cohort first, then maturedPending, then the bond. The caller has
    ///      checked that they suffice. Vested-unclaimed shares are never taken.
    function _take(Bank storage b, uint256 need) private returns (uint256 fromBank, uint256 fromBond) {
        // [MU-028] youngest (highest epoch) first
        for (uint256 r; r < RING && need > 0; ++r) {
            uint256 best = RING;
            for (uint256 k; k < RING; ++k) {
                Cohort storage c = b.cohorts[k];
                if (c.shares > 0 && (best == RING || c.epoch > b.cohorts[best].epoch)) best = k;
            }
            if (best == RING) break;
            Cohort storage y = b.cohorts[best];
            uint256 x = need < y.shares ? need : y.shares;
            y.shares -= uint128(x);
            need -= x;
            fromBank += x;
        }
        // [MU-088] then maturedPending
        uint256 m = need < b.maturedPending ? need : b.maturedPending;
        b.maturedPending -= uint128(m);
        need -= m;
        fromBank += m;
        // [MU-029] then the bond; vestedUnclaimed is never burnable
        b.bondShares -= uint128(need);
        fromBond = need;
    }

    function _unvested(Bank storage b) private view returns (uint256 u) {
        for (uint256 k; k < RING; ++k) {
            u += b.cohorts[k].shares;
        }
        u += b.maturedPending;
    }

    /// @dev Saturating cast to uint128 (F-003: pay and checkpoint arithmetic never reverts).
    function _sat(uint256 x) private pure returns (uint128) {
        // [MU-038] saturate, never revert
        return x > type(uint128).max ? type(uint128).max : uint128(x);
    }
}
