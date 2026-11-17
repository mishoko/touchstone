// SPDX-License-Identifier: MIT
pragma solidity 0.8.26;

import {Test} from "forge-std/Test.sol";
import {IPoolManager} from "@uniswap/v4-core/src/interfaces/IPoolManager.sol";
import {PoolKey} from "@uniswap/v4-core/src/types/PoolKey.sol";
import {PoolId} from "@uniswap/v4-core/src/types/PoolId.sol";
import {StateLibrary} from "@uniswap/v4-core/src/libraries/StateLibrary.sol";
import {TickMath} from "@uniswap/v4-core/src/libraries/TickMath.sol";
import {FullMath} from "@uniswap/v4-core/src/libraries/FullMath.sol";
import {SwapParams} from "@uniswap/v4-core/src/types/PoolOperation.sol";
import {PoolSwapTest} from "@uniswap/v4-core/src/test/PoolSwapTest.sol";
import {VaultHarness} from "../../utils/VaultHarness.sol";
import {MockRefPool} from "../../utils/MockRefPool.sol";
import {MockToken} from "../../utils/Stubs.sol";
import {ExpectedFlows} from "../../utils/ExpectedFlows.sol";
import {ITouchstoneHook} from "../../../src/interfaces/ITouchstoneHook.sol";
import {TouchstoneVault} from "../../../src/TouchstoneVault.sol";
import {Holdings} from "../../../src/types/Holdings.sol";
import {PreSwap} from "../../../src/types/PreSwap.sol";
import {Tier} from "../../../src/types/Tier.sol";
import {Bank, Cohort, RING, V} from "../../../src/types/Bank.sol";
import {ExecLedger} from "../../../src/libraries/ExecLedger.sol";
import {FeeBank} from "../../../src/libraries/FeeBank.sol";
import {Ladder} from "../../../src/libraries/Ladder.sol";

/// @notice The handler: deposits, withdrawals, time (snapshotted steps and silent jumps), keep-alive refreshes, the
///         curator's actions (`setPositions` with an optional move and pre-swap, `rebalanceSwap`), third-party swaps,
///         reference moves and `breakRef` (every failure mode of the scripted reference). Every withdrawal is checked against the exact pro-rata share computed from the
///         PoolManager's state (I-2) and against the revert allow-list (I-10); the results are kept in ghosts.
contract VaultHandler is Test {
    using StateLibrary for IPoolManager;

    IPoolManager internal immutable manager;
    VaultHarness internal immutable hv;
    ITouchstoneHook internal immutable hook;
    MockRefPool internal immutable refPool;
    MockToken internal immutable token0;
    MockToken internal immutable token1;
    PoolSwapTest internal immutable swapRouter;
    PoolKey internal key;
    PoolId internal immutable poolId;
    uint256 internal immutable blocktime;
    int24 internal immutable center;
    uint256 internal immutable swapMax0;
    uint256 internal immutable swapMax1;
    address[3] internal actors;

    // ghosts
    uint256 public withdrawals;
    uint256 public withdrawalsWithL;
    uint256 public placements;
    uint256 public breachesDriven;
    uint256 public exitedWithdrawals;
    uint256 public bankSeeds;
    uint256 public gateOpenVests;
    uint256 public i2Failures;
    string public i2Why;
    uint256 public i10Failures;
    bytes4 public i10Selector;
    uint256 public pends;
    uint256 public settledByRead;
    uint256 public settledByFallback;
    uint256 public withdrawalsSettling;
    uint256 public curatorSwaps;
    uint256 public settledReal;
    bool internal _realPending;
    uint256 public actionFailures;
    bytes4 public actionSelector;
    uint256 public curatorPlacements;
    uint256 public placementsMoved;
    uint256 public placementsPreSwapped;
    /// @notice Calls that reached the vault (non-vacuity of the random campaign, value review item 6).
    uint256 public rebalanceCalls;
    uint256 public placeCalls;

    constructor(
        IPoolManager m,
        VaultHarness v,
        address h,
        MockRefPool r,
        MockToken t0,
        MockToken t1,
        PoolKey memory k,
        uint256 bt,
        int24 c,
        uint256 max0,
        uint256 max1
    ) {
        (manager, hv, hook, refPool, token0, token1) = (m, v, ITouchstoneHook(h), r, t0, t1);
        key = k;
        poolId = PoolId.wrap(keccak256(abi.encode(k)));
        (blocktime, center, swapMax0, swapMax1) = (bt, c, max0, max1);
        swapRouter = new PoolSwapTest(m);
        t0.mint(address(this), 1e40);
        t1.mint(address(this), 1e40);
        t0.approve(address(swapRouter), type(uint256).max);
        t1.approve(address(swapRouter), type(uint256).max);
        for (uint256 i; i < 3; ++i) {
            address a = address(uint160(0xA11CE0 + i));
            actors[i] = a;
            t0.mint(a, 1e40);
            t1.mint(a, 1e40);
            vm.startPrank(a);
            t0.approve(address(v), type(uint256).max);
            t1.approve(address(v), type(uint256).max);
            vm.stopPrank();
        }
    }

    // ------------------------------------------------------------------ flows

    function deposit(uint256 who, uint256 s) external {
        address a = actors[who % 3];
        uint256 S = hv.totalSupply();
        uint256 cap = uint256(hv.bondShares()) * 1e18 / hv.tier().BOND_RATIO;
        if (cap <= S) return;
        s = bound(s, 1, _min(cap - S, S / 4));
        vm.prank(a);
        try hv.deposit(s, type(uint256).max, type(uint256).max, a) {} catch {}
    }

    function withdraw(uint256 who, uint256 s) external {
        address a = actors[who % 3];
        uint256 bal = hv.balanceOf(a);
        if (bal == 0) return;
        s = bound(s, 1, bal);
        // the expectation, from the PoolManager's state in this block (the prologue swaps nothing; collect-first
        // sweeps the fees computed here) and the bank's closed-form drip (the checkpoint burns it before the flow)
        Holdings memory h = hv.holdings();
        // settlement step 0 burns first (the drip it may restart owes nothing more in this block), then the drip
        (uint256 burnS, bool due, bool ok) = _expectedSettleBurn();
        uint256 S = hv.totalSupply() - burnS - _dripOwed(hv.bankFull());
        (uint256 f0, uint256 f1) = ExpectedFlows.fees(manager, poolId, address(hv), h.lo, h.hi);
        uint128 dL = uint128(FullMath.mulDiv(h.L, s, S));
        (uint256 r0, uint256 r1) = ExpectedFlows.legs(manager, poolId, h.lo, h.hi, dL, false);
        uint256 e0 = FullMath.mulDiv(h.idle0 + f0, s, S) + r0;
        uint256 e1 = FullMath.mulDiv(h.idle1 + f1, s, S) + r1;
        uint256 b0 = token0.balanceOf(a);
        uint256 b1 = token1.balanceOf(a);
        (uint128 posBefore,,) = manager.getPositionInfo(poolId, address(hv), h.lo, h.hi, bytes32(0));
        uint256 vestedBefore = hv.bankFull().vestedUnclaimed;
        vm.prank(a);
        try hv.withdraw(s, 0, 0, a) {
            ++withdrawals;
            if (h.L > 0) ++withdrawalsWithL;
            (,,,,, uint8 mode,) = hv.bank();
            if (mode == 2) ++exitedWithdrawals;
            if (hv.bankFull().vestedUnclaimed > vestedBefore) ++gateOpenVests;
            if (due) {
                ++withdrawalsSettling;
                _countSettle(ok);
            }
            if (token0.balanceOf(a) - b0 != e0) _i2("token0 payout");
            if (token1.balanceOf(a) - b1 != e1) _i2("token1 payout");
            (uint128 posAfter,,) = manager.getPositionInfo(poolId, address(hv), h.lo, h.hi, bytes32(0));
            if (posBefore - posAfter != dL) _i2("liquidity removed from the PoolManager position");
            if (h.L - hv.holdings().L != dL) _i2("ledger L");
        } catch (bytes memory err) {
            // The spec's allow-list (ZeroShares, Slippage, ERC20InsufficientBalance) is the caller's own errors; this
            // handler asks for 1 <= s <= balance with min = 0, so in its domain every revert is a failure (in
            // particular an ERC20InsufficientBalance can only come from a checkpoint burn the vault cannot cover).
            ++i10Failures;
            i10Selector = bytes4(err);
        }
    }

    /// @notice Puts vault-held shares into the bank, carved out of the bond (they stay held by the vault): a due
    ///         cohort, maturedPending, or a forfeit pool that starts dripping now. So the checkpoint's live read,
    ///         drip burn, breach forfeit and vest run inside the flows.
    function seedBank(uint256 kind, uint256 amt) external {
        Bank memory b = hv.bankFull();
        amt = bound(amt, 1, b.bondShares / 1000);
        uint32 ne = uint32(vm.getBlockNumber() / hv.tier().EPOCH);
        uint256 k = kind % 3;
        if (k == 0) {
            uint32 e = ne >= V + 1 ? ne - uint32(V + 1) : ne; // due when ne >= e + 1 + V
            Cohort memory c = b.cohorts[e % RING];
            if (c.shares > 0 && c.epoch != e) return;
            b.cohorts[e % RING] = Cohort(e, c.shares + uint128(amt));
        } else if (k == 1) {
            b.maturedPending += uint128(amt);
        } else {
            if (b.forfeitPool > 0) return; // a restart burns the drip owed first; keep the handler's state reachable
            (b.forfeitPool, b.forfeitTotal, b.forfeitStart) = (uint128(amt), uint128(amt), uint40(vm.getBlockNumber()));
        }
        b.bondShares -= uint128(amt);
        hv.setBank(b);
        ++bankSeeds;
    }

    /// @notice A pending curator action at this block (the actions that record one are built later; this writes it
    ///         through the harness seam): a loss of `d` token0, an immediate charge and a reserve value, with the reserve's
    ///         shares carved out of the bond, part of them marked as the bond's.
    function pend(uint256 d, uint256 charge0, uint256 reserve, uint256 shares, uint256 bondPart) external {
        (uint40 tA,,,,) = hv.pendingAction();
        Bank memory b = hv.bankFull();
        if (tA != 0 || b.reserveShares != 0 || b.bondShares < 2000) return;
        Holdings memory a = hv.holdings();
        d = bound(d, 0, 1e16);
        Holdings memory before = Holdings(uint128(a.idle0 + d), a.idle1, a.L, a.lo, a.hi);
        uint128 rs = uint128(bound(shares, 1, b.bondShares / 1000));
        b.bondShares -= rs;
        (b.reserveShares, b.reserveBond, b.reserveForfeit) = (rs, uint128(bound(bondPart, 0, rs)), false);
        hv.setBank(b);
        hv.setPending(
            uint40(vm.getBlockTimestamp()), before, a, uint128(bound(charge0, 0, 1e16)), uint128(bound(reserve, 0, 1e16))
        );
        ++pends;
        _realPending = false;
    }

    /// @notice The open `settle()`; it never reverts with full gas.
    function settle() external {
        (, bool due, bool ok) = _expectedSettleBurn();
        try hv.settle() {
            if (due) _countSettle(ok);
        } catch (bytes memory err) {
            ++i10Failures;
            i10Selector = bytes4(err);
        }
    }

    /// @notice The curator's `rebalanceSwap` of up to a quarter of the input token's idle balance. On even `align` the
    ///         reference is first set flat at the pool's tick (its live TWAP equals it at once), so the fence passes;
    ///         otherwise the action meets whatever the market and the reference are. Every revert must be one of the
    ///         action's own guards (or the bank's gate, or the pending markout of a seam action).
    function rebalance(bool z, uint256 amt, uint256 align) external {
        if (align % 2 == 0) {
            (, int24 t,,) = manager.getSlot0(poolId);
            uint32[] memory st = new uint32[](1);
            int24[] memory tk = new int24[](1);
            (st[0], tk[0]) = (1, t);
            refPool.setMode(MockRefPool.Mode.Ok);
            refPool.setPath(st, tk);
        }
        Holdings memory h = hv.holdings();
        // swap the other token if this one is (nearly) all deployed
        if ((z ? h.idle0 : h.idle1) < 4) z = !z;
        uint256 idle = z ? h.idle0 : h.idle1;
        if (idle < 4) return;
        amt = bound(amt, 1, idle / 4);
        ++rebalanceCalls;
        // the state each guard may legitimately revert in (the same predicates as `place`; no move, no placement)
        Tier memory tr = hv.tier();
        (uint160 sp, int24 tNow,,) = manager.getSlot0(poolId);
        (bool refOk, int24 ref) = hook.tryRef();
        Expect memory x = _placeExpect(tr, h, 0, sp, tNow, tNow, ref, refOk, true, false);
        uint256 frac;
        if (refOk) {
            // the swap's fraction of NAV at the reference on the ledger holdings before the action; uncollected fees
            // only raise the vault's NAV, so this over-states the fraction (a SwapCap below it is a wrong cap)
            frac = ExecLedger.swapFrac(h, z, amt, TickMath.getSqrtPriceAtTick(ref));
            // half the calls stay under the per-action cap (at tick 196,000 an idle/4 swap exceeds it), so the random
            // campaign executes swaps rather than only exercising the cap
            if (amt % 2 == 0 && frac > tr.MAX_SWAP_ACTION) {
                amt = amt * tr.MAX_SWAP_ACTION / frac;
                if (amt == 0) return;
                frac = ExecLedger.swapFrac(h, z, amt, TickMath.getSqrtPriceAtTick(ref));
            }
        }
        bool overCap = frac > tr.MAX_SWAP_ACTION || hv.scalars().epochSwapFrac + frac > tr.MAX_SWAP_EPOCH;
        (, bool due, bool ok) = _expectedSettleBurn();
        vm.prank(hv.curator());
        try hv.rebalanceSwap(z, uint128(amt), type(uint256).max) {
            ++curatorSwaps;
            // settled first (prologue step 0), then this action's own record replaced it
            if (due) _countSettled(ok);
            _realPending = true;
            (uint40 tA,,,,) = hv.pendingAction();
            if (tA != vm.getBlockTimestamp()) _i2("the action left no pending record");
        } catch (bytes memory err) {
            bytes4 sel = bytes4(err);
            // value review V-1 (F-043): each guard only in the state that legitimises it, as in `place`
            bool allowed = (sel == TouchstoneVault.Fence.selector && !x.fenceOk)
                || (sel == ITouchstoneHook.RefUnreadable.selector && !refOk)
                || (sel == TouchstoneVault.Cooldown.selector && x.cooling)
                || (sel == TouchstoneVault.MaxSwaps.selector && x.swapsOut)
                || (sel == TouchstoneVault.SwapCap.selector && (overCap || !refOk))
                || (sel == FeeBank.Insolvent.selector && _coverShort())
                // a pending record blocks an action only when the seam wrote it (a real one is due by the cooldown)
                || (sel == FeeBank.MarkoutPending.selector && !_realPending)
                || (sel == Ladder.BadMode.selector && x.mayExit);
            if (!allowed) {
                ++actionFailures;
                actionSelector = sel;
            }
        }
    }

    /// @notice The curator's `setPositions` on a W-wide aligned range containing the (post-move) spot and the live
    ///         reference. On even `align` the reference is first set flat at the pool's tick. A quarter of the calls
    ///         use the vault's own move: half of them by ±1…40 ticks, half by one unit of price; a third add a pre-swap of up to an eighth of
    ///         the input token's idle. Every revert must be one of the action's own guards, each allowed only in the
    ///         state that legitimises it; a success leaves the PoolManager position equal to the ledger, the old range
    ///         empty, the claims equal to idle, NORMAL and `placedByCurator`.
    function place(uint256 off, uint256 align, uint256 pre, uint256 move) external {
        Tier memory tr = hv.tier();
        (uint160 sp, int24 t,,) = manager.getSlot0(poolId);
        if (align % 2 == 0) {
            uint32[] memory st = new uint32[](1);
            int24[] memory tk = new int24[](1);
            (st[0], tk[0]) = (1, t);
            refPool.setMode(MockRefPool.Mode.Ok);
            refPool.setPath(st, tk);
        }
        uint160 moveTo;
        int24 tm = t;
        if (move % 4 == 0) {
            int24 dm;
            if ((move >> 2) % 2 == 0) {
                dm = int24(int256(bound(move >> 3, 0, 79))) - 40;
                if (dm >= 0) ++dm; // ±1 … ±40 ticks (through third-party depth this usually meets the swap cap)
            }
            if (dm != 0) {
                tm = t + dm;
                // one unit above sqrtAt(tm): the pool tick ends at tm from either side (a move down onto an initialized
                // tick boundary would otherwise leave tm − 1)
                moveTo = TickMath.getSqrtPriceAtTick(tm) + 1;
            } else {
                // a one-unit move from the current price (≈ free at any depth)
                moveTo = sp + 1;
                tm = TickMath.getTickAtSqrtPrice(moveTo);
            }
        }
        (bool ok, int24 ref) = hook.tryRef();
        if (!ok) ref = tm;
        int24 lo;
        {
            int24 loMax = _floor10(tm < ref ? tm : ref);
            int24 loMin = _floor10(tm < ref ? ref : tm) + 10 - tr.W_TICKS;
            lo = loMin > loMax ? loMax : loMin + int24(int256(bound(off, 0, uint256(int256((loMax - loMin) / 10))))) * 10;
        }
        PreSwap memory ps;
        Holdings memory h = hv.holdings();
        if (pre % 3 == 0) {
            bool z = (pre >> 2) % 2 == 0;
            uint256 idle = z ? h.idle0 : h.idle1;
            if (idle >= 8) ps = PreSwap(true, z, uint128(bound(pre >> 8, 1, idle / 8)));
        }
        Expect memory x = _placeExpect(tr, h, lo, sp, t, tm, ref, ok, ps.active, moveTo != 0);
        // without a move the pre-swap alone spends the per-action budget: SwapCap only above it (V-1, F-043); a move's
        // input depends on the depth it crosses, so with a move SwapCap stays allowed
        bool capOk = moveTo != 0 || !ok;
        if (!capOk && ps.active) {
            uint256 f = ExecLedger.swapFrac(h, ps.zeroForOne, ps.amountIn, TickMath.getSqrtPriceAtTick(ref));
            capOk = f > tr.MAX_SWAP_ACTION || hv.scalars().epochSwapFrac + f > tr.MAX_SWAP_EPOCH;
        }
        ++placeCalls;
        (, bool due, bool okS) = _expectedSettleBurn();
        vm.prank(hv.curator());
        try hv.setPositions(lo, lo + tr.W_TICKS, ps, moveTo, type(uint256).max) {
            ++curatorPlacements;
            if (moveTo != 0) ++placementsMoved;
            if (ps.active) ++placementsPreSwapped;
            if (due) _countSettled(okS);
            _realPending = true;
            _checkPlaced(h, lo, lo + tr.W_TICKS);
        } catch (bytes memory err) {
            bytes4 sel = bytes4(err);
            bool allowed = (sel == TouchstoneVault.Fence.selector && !x.fenceOk)
                || (sel == ITouchstoneHook.RefUnreadable.selector && !ok)
                || (sel == TouchstoneVault.Cooldown.selector && x.cooling)
                || (sel == FeeBank.MarkoutPending.selector && !_realPending)
                || (sel == TouchstoneVault.MaxSwaps.selector && ps.active && x.swapsOut)
                || (sel == TouchstoneVault.SwapCap.selector && (ps.active || moveTo != 0) && capOk)
                || (sel == TouchstoneVault.DeltaMin.selector && x.deltaShort)
                || (sel == Ladder.BadMode.selector && x.mayExit)
                // the bank's gate, only once unvested pay and the bond are below the 2× rule's floor
                || (sel == FeeBank.Insolvent.selector && _coverShort());
            if (!allowed) {
                ++actionFailures;
                actionSelector = sel;
            }
        }
    }

    /// @dev Unvested pay plus the bond below `S·BOND_RATIO/2`: the 2× rule covers any single action above that floor,
    ///      so an `Insolvent` revert is legitimate only below it.
    function _coverShort() internal view returns (bool) {
        Bank memory b = hv.bankFull();
        uint256 cover = uint256(b.maturedPending) + b.bondShares;
        for (uint256 k; k < RING; ++k) cover += b.cohorts[k].shares;
        return cover * 2e18 < hv.totalSupply() * hv.tier().BOND_RATIO;
    }

    /// @dev The states that legitimise each placement revert, read before the call.
    struct Expect {
        bool fenceOk;
        bool cooling;
        bool swapsOut;
        bool deltaShort;
        bool mayExit;
    }

    function _placeExpect(
        Tier memory tr,
        Holdings memory h,
        int24 lo,
        uint160 sp,
        int24 t,
        int24 tm,
        int24 ref,
        bool ok,
        bool preSwap,
        bool moved
    ) internal view returns (Expect memory x) {
        int256 dist = int256(tm) - ref;
        x.fenceOk = ok && dist <= tr.REF_BAND && -dist <= tr.REF_BAND;
        uint256 la = hv.lastAction();
        x.cooling = la != 0 && vm.getBlockNumber() < la + tr.COOLDOWN;
        x.swapsOut = hv.scalars().swapCount >= tr.MAX_SWAPS;
        (,,,,, uint8 mode,) = hv.bank();
        x.mayExit = mode == Ladder.EXITED || hook.obs().pendingBreaches > 0;
        // δ_after on the idle the removal leaves (fees swept first) at this spot and the live reference; a move or a
        // pre-swap changes the inventory and the spot, so either may legitimately leave δ short
        if (preSwap || moved || !ok) {
            x.deltaShort = true;
        } else {
            (uint256 f0, uint256 f1) = ExpectedFlows.fees(manager, poolId, address(hv), h.lo, h.hi);
            (uint256 r0, uint256 r1) = ExpectedFlows.legs(manager, poolId, h.lo, h.hi, h.L, false);
            Holdings memory idle = Holdings(uint128(h.idle0 + f0 + r0), uint128(h.idle1 + f1 + r1), 0, 0, 0);
            (,, uint64 d) = ExecLedger.placementSize(idle, lo, lo + tr.W_TICKS, sp, TickMath.getSqrtPriceAtTick(ref));
            x.deltaShort = d < tr.DELTA_MIN;
        }
        t;
    }

    /// @dev After a successful placement: the PoolManager position equals the ledger on the new range, the old range
    ///      (if different) is empty, the claims equal idle, the mode is NORMAL and the range is the curator's.
    function _checkPlaced(Holdings memory old, int24 lo, int24 hi) internal {
        Holdings memory h = hv.holdings();
        (uint40 tA,,,,) = hv.pendingAction();
        if (tA != vm.getBlockTimestamp()) _i2("the placement left no pending record");
        if (h.lo != lo || h.hi != hi) _i2("the ledger range");
        (uint128 liq,,) = manager.getPositionInfo(poolId, address(hv), lo, hi, bytes32(0));
        if (liq != h.L) _i2("the PoolManager position differs from the ledger L");
        if (old.L > 0 && (old.lo != lo || old.hi != hi)) {
            (uint128 l0,,) = manager.getPositionInfo(poolId, address(hv), old.lo, old.hi, bytes32(0));
            if (l0 != 0) _i2("the old position was not removed");
        }
        if (manager.balanceOf(address(hv), uint256(uint160(address(token0)))) != h.idle0) _i2("claims0 != idle0");
        if (manager.balanceOf(address(hv), uint256(uint160(address(token1)))) != h.idle1) _i2("claims1 != idle1");
        Bank memory b = hv.bankFull();
        if (b.mode != Ladder.NORMAL || !b.placedByCurator) _i2("the placement's mode row");
    }

    // ------------------------------------------------------------------ the market and time

    function swap(bool zeroForOne, uint256 amount, uint256 limit) external {
        amount = bound(amount, 1e3, zeroForOne ? swapMax0 : swapMax1);
        (, int24 t,,) = manager.getSlot0(poolId);
        int24 d = int24(int256(bound(limit, 1, 400)));
        int24 lim = zeroForOne ? t - d : t + d;
        if (lim < center - 5900) lim = center - 5900;
        if (lim > center + 5900) lim = center + 5900;
        if ((zeroForOne && lim >= t) || (!zeroForOne && lim <= t)) return;
        swapRouter.swap(
            key,
            SwapParams(zeroForOne, -int256(amount), TickMath.getSqrtPriceAtTick(lim)),
            PoolSwapTest.TestSettings({takeClaims: false, settleUsingBurn: false}),
            ""
        );
    }

    /// @notice 85%: 1-3 blocks, each snapshotted; 15%: a silent jump of 37-120 blocks.
    function advance(uint256 seed) external {
        if (seed % 100 < 85) {
            uint256 n = 1 + (seed >> 8) % 3;
            for (uint256 i; i < n; ++i) {
                _roll(1);
                hook.snapshotIfNewBlock();
            }
        } else {
            _roll(37 + (seed >> 8) % 84);
        }
    }

    /// @notice Holds the reference outside the vault's range, observed, until one more breach latches (bounded).
    function driveBreach() external {
        Holdings memory h = hv.holdings();
        refPool.setMode(MockRefPool.Mode.Ok);
        refPool.setTick(h.hi + 1000 > center + 1500 ? h.lo - 1000 : h.hi + 1000);
        uint32 seq = hook.obs().breachSeq;
        for (uint256 i; i < 40 && hook.obs().breachSeq == seq; ++i) {
            _roll(10);
            try hook.refreshRef() {} catch {}
        }
        if (hook.obs().breachSeq > seq) ++breachesDriven;
    }

    function refresh() external {
        try hook.refreshRef() {} catch {}
    }

    /// @notice Moves the reference by up to ±1,500 ticks from the pool's centre (breaches latch when it leaves the
    ///         vault's range; the reference starts in range).
    function moveRef(uint256 d) external {
        int24 t = center + int24(int256(bound(d, 0, 3000))) - 1500;
        refPool.setTick(t);
    }

    /// @notice Every failure mode of the scripted reference, and back (half the calls restore it).
    function breakRef(uint256 m) external {
        uint256 k = m % 10;
        if (k < 5) refPool.setMode(MockRefPool.Mode.Ok);
        else if (k == 5) refPool.setMode(MockRefPool.Mode.Old);
        else if (k == 6) refPool.setMode(MockRefPool.Mode.Revert);
        else if (k == 7) refPool.setMode(MockRefPool.Mode.Short);
        else if (k == 8) refPool.setMode(MockRefPool.Mode.GasBomb);
        else refPool.setMode(MockRefPool.Mode.ReturnBomb);
    }

    // ------------------------------------------------------------------ helpers

    /// @dev The drip owed at the current block, by the closed form burnedSoFar = min(T, T·(now − start)/D).
    function _dripOwed(Bank memory b) internal view returns (uint256) {
        if (b.forfeitTotal == 0 || b.forfeitPool == 0) return 0;
        uint256 el = vm.getBlockNumber() > b.forfeitStart ? vm.getBlockNumber() - b.forfeitStart : 0;
        uint256 soFar = b.dripBlocks == 0 ? b.forfeitTotal : _min(b.forfeitTotal, uint256(b.forfeitTotal) * el / b.dripBlocks);
        uint256 spent = b.forfeitTotal - b.forfeitPool;
        return soFar > spent ? _min(soFar - spent, b.forfeitPool) : 0;
    }

    /// @dev The settlement burn prologue step 0 will make in this block, computed from the hook's own window read and
    ///         `ExecLedger.settleExtra` on the pre-collect holdings and supply (the whole reserve if the read fails).
    function _expectedSettleBurn() internal view returns (uint256 burnS, bool due, bool ok) {
        (uint40 tA, Holdings memory b, Holdings memory a, uint128 c0, uint128 r) = hv.pendingAction();
        uint256 ts = vm.getBlockTimestamp();
        uint256 H = hv.tier().H_MARKOUT;
        if (tA == 0 || ts < tA + 2 * blocktime + H) return (0, false, false);
        due = true;
        uint256 x = ts - tA - blocktime;
        int24 tick;
        (ok, tick) = hook.tryWindow(uint32(x), uint32(x - H));
        uint256 rs = hv.bankFull().reserveShares;
        if (!ok) return (rs, true, false);
        (,, burnS) = ExecLedger.settleExtra(b, a, tick, hv.tier().FEE_TICKS, c0, r, hv.holdings(), hv.totalSupply(), rs);
        if (burnS > rs) burnS = rs;
    }

    function _countSettle(bool ok) internal {
        (uint40 tA,,,,) = hv.pendingAction();
        if (tA != 0 || hv.bankFull().reserveShares != 0) _i2("a due settlement left the record or the reserve");
        _countSettled(ok);
    }

    function _countSettled(bool ok) internal {
        if (ok) ++settledByRead;
        else ++settledByFallback;
        if (_realPending) ++settledReal;
        _realPending = false;
    }

    function _i2(string memory why) internal {
        // keep the first reason: later checks of the same call may only echo it
        if (i2Failures++ == 0) i2Why = why;
    }

    function _roll(uint256 n) internal {
        vm.roll(vm.getBlockNumber() + n);
        vm.warp(vm.getBlockTimestamp() + n * blocktime);
    }

    function _floor10(int24 t) internal pure returns (int24) {
        int24 f = (t / 10) * 10;
        return (t < 0 && f != t) ? f - 10 : f;
    }

    function _min(uint256 a, uint256 b) internal pure returns (uint256) {
        return a < b ? a : b;
    }
}
