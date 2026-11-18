// SPDX-License-Identifier: MIT
pragma solidity 0.8.26;

import {IPoolManager} from "@uniswap/v4-core/src/interfaces/IPoolManager.sol";
import {Extsload} from "@uniswap/v4-core/src/Extsload.sol";
import {IUnlockCallback} from "@uniswap/v4-core/src/interfaces/callback/IUnlockCallback.sol";
import {IHooks} from "@uniswap/v4-core/src/interfaces/IHooks.sol";
import {PoolKey} from "@uniswap/v4-core/src/types/PoolKey.sol";
import {PoolId} from "@uniswap/v4-core/src/types/PoolId.sol";
import {StateLibrary} from "@uniswap/v4-core/src/libraries/StateLibrary.sol";
import {Currency} from "@uniswap/v4-core/src/types/Currency.sol";
import {BalanceDelta} from "@uniswap/v4-core/src/types/BalanceDelta.sol";
import {ModifyLiquidityParams, SwapParams} from "@uniswap/v4-core/src/types/PoolOperation.sol";
import {TickMath} from "@uniswap/v4-core/src/libraries/TickMath.sol";
import {FullMath} from "@uniswap/v4-core/src/libraries/FullMath.sol";
import {ReentrancyGuardTransient} from "@openzeppelin/contracts/utils/ReentrancyGuardTransient.sol";
import {ITouchstoneFactory} from "./interfaces/ITouchstoneFactory.sol";
import {IVaultEntries, ITouchstoneVaultExtension} from "./interfaces/ITouchstoneVaultExtension.sol";
import {Obs} from "./types/Obs.sol";
import {Entry} from "./types/Entry.sol";
import {Holdings} from "./types/Holdings.sol";
import {Markout} from "./types/Markout.sol";
import {PreSwap} from "./types/PreSwap.sol";
import {Bank, Op, RING, V} from "./types/Bank.sol";
import {EpochRecord, F_BREACH, F_REF_STALE, F_DEFERRED, F_UNVERIFIED} from "./types/EpochRecord.sol";
import {Ladder} from "./libraries/Ladder.sol";
import {ExecLedger} from "./libraries/ExecLedger.sol";
import {FeeBank} from "./libraries/FeeBank.sol";
import {Placement} from "./libraries/Placement.sol";
import {VaultCommon} from "./TouchstoneVaultBase.sol";

/// @title TouchstoneVault
/// @notice A single-position Uniswap v4 vault whose curator is paid from a bank of deferred shares. Shares are pro
///         rata in kind over three buckets kept in storage (idle token0, idle token1, liquidity); idle balances are
///         PoolManager ERC-6909 claims tracked internally, so unsolicited transfers of tokens, claims or shares are
///         never read. Every entry point runs inside the vault's own `unlock`: the prologue snapshots the hook's
///         clock, collects the position's fees into idle, then checkpoints; the mode gate runs after the prologue.
/// @dev The vault carries the hot path (the curator actions, the settlement, the prologue and the checkpoint); its
///      cold paths (`seed`, `deposit`, `withdraw`, `claim`, `topUpBond`, `retire`, `releaseBond`, `poke`, `processBreach`) live in its per-vault extension (`TouchstoneVaultExtension`), which
///      the fallback reaches by `delegatecall` on the same storage (`VaultStorage`). The vault has no state views:
///      every field is read through `extsload` at the slots of its storage-layout snapshot (the Lens decodes them),
///      and the tier through `TIER_HASH`.
contract TouchstoneVault is VaultCommon, Extsload, IUnlockCallback, ReentrancyGuardTransient {
    using StateLibrary for IPoolManager;

    /// @notice This vault's extension (spec §4.12): its fallback forwards the moved entry points there.
    address public immutable EXTENSION;

    constructor() {
        address ext = ITouchstoneFactory(msg.sender).pending().extension;
        EXTENSION = ext;
        // consistency with the extension the factory created for this vault (the binding is the factory's code-hash
        // check): [MU-265] it has code, [MU-266] it serves this vault, [MU-267] it carries this configuration
        if (ext.code.length == 0) revert BadExtension();
        if (ITouchstoneVaultExtension(ext).VAULT() != address(this)) revert BadExtension();
        if (ITouchstoneVaultExtension(ext).CONFIG_HASH() != CONFIG) revert BadExtension();
    }

    // ------------------------------------------------------------------ entry points

    /// @notice The curator's placement on `[lo, hi)`, charged its implementation shortfall (see `_action`).
    ///         `moveTo = 0` means no move.
    function setPositions(int24 lo, int24 hi, PreSwap calldata preSwap, uint160 moveTo, uint256 maxCharge)
        external
        nonReentrant
    {
        _act(Entry.SetPositions, abi.encode(lo, hi, preSwap, moveTo, maxCharge));
    }

    /// @notice The curator's exact-input swap from idle, charged its implementation shortfall: the action of
    ///         `_action` with the swap alone (no removal, no move, no placement).
    function rebalanceSwap(bool zeroForOne, uint128 amountIn, uint256 maxCharge) external nonReentrant {
        _act(
            Entry.RebalanceSwap,
            abi.encode(int24(0), int24(0), PreSwap(true, zeroForOne, amountIn), uint160(0), maxCharge)
        );
    }

    function _act(Entry entry, bytes memory args) internal {
        _onlyCurator();
        MANAGER.unlock(abi.encode(entry, msg.sender, args));
    }

    /// @notice Settles a pending curator action whose markout window has closed (a no-op otherwise), then checkpoints.
    function settle() external nonReentrant {
        MANAGER.unlock(abi.encode(Entry.Settle, msg.sender, ""));
    }

    /// @notice The moved entry points (`seed`, `deposit`, `withdraw` and the six P4 entries; `IVaultEntries`), forwarded to the extension by
    ///         `delegatecall` with their calldata; any other selector and empty calldata revert. The vault's one
    ///         reentrancy lock is held across the whole call, and the extension's output is returned raw (so the
    ///         lock's release runs on return).
    // [MU-160] every entry point holds the reentrancy lock across its whole unlock
    fallback(bytes calldata data) external nonReentrant returns (bytes memory) {
        bytes4 sel = bytes4(data);
        // [MU-264] only the allow-list (a call shorter than a selector is refused too: no allow-listed selector ends in
        // a zero byte, so the length check is defence in depth and carries no mutant)
        if (
            data.length < 4
                || (sel != IVaultEntries.seed.selector && sel != IVaultEntries.deposit.selector
                    && sel != IVaultEntries.withdraw.selector && sel != IVaultEntries.claim.selector
                    && sel != IVaultEntries.topUpBond.selector && sel != IVaultEntries.retire.selector
                    && sel != IVaultEntries.releaseBond.selector && sel != IVaultEntries.poke.selector
                    && sel != IVaultEntries.processBreach.selector)
        ) revert UnknownSelector();
        return _delegate(data);
    }

    /// @dev Runs `data` on the extension by `delegatecall`; a revert is bubbled verbatim.
    function _delegate(bytes memory data) internal returns (bytes memory ret) {
        bool ok;
        (ok, ret) = EXTENSION.delegatecall(data);
        if (!ok) {
            assembly ("memory-safe") {
                revert(add(ret, 0x20), mload(ret))
            }
        }
    }

    /// @dev A moved flow's body, run on the extension's `dispatch` (by `delegatecall`); returns its `bytes` result.
    function _extension(Entry entry, address payer, bytes memory args) internal returns (bytes memory) {
        return abi.decode(_delegate(abi.encodeCall(ITouchstoneVaultExtension.dispatch, (entry, payer, args))), (bytes));
    }

    function unlockCallback(bytes calldata data) external returns (bytes memory) {
        // [MU-158] only the PoolManager calls back, and it calls back only the unlocker: this makes `payer` trustworthy
        if (msg.sender != address(MANAGER)) revert OnlyPoolManager();
        (Entry entry, address payer, bytes memory args) = abi.decode(data, (Entry, address, bytes));
        // [MU-268] seed runs at once in the extension: no step 0, prologue, checkpoint or mode gate (spec §4.6 seed)
        if (entry == Entry.Seed) return _extension(entry, payer, args);
        if (!_seeded) revert NotSeeded(); // [MU-273] nothing but seed runs before seed
        // [MU-185] step 0, before the snapshot and collect-first (the binding order; since s15 the settlement reads only its
        // record, so the order matters again once the checkpoint mints base shares)
        _settle();
        Obs memory o = _prologue();
        // [MU-037] [MU-278] every entry point checkpoints
        (bool liveRead, int24 liveTick) = _checkpoint(o);
        // [MU-073] the mode gate runs after the prologue checkpoint, which may have moved the mode to EXITED
        if (!Ladder.allowed(_bank.mode, uint8(entry))) revert Ladder.BadMode();
        return _dispatch(entry, payer, args, liveRead, liveTick);
    }

    /// @dev The entry points of this build. A test harness extends it; every other entry is unreachable here.
    ///      `liveRead`/`liveTick`: the checkpoint's live reference read in this call, if it made one that succeeded.
    function _dispatch(Entry entry, address payer, bytes memory args, bool liveRead, int24 liveTick)
        internal
        virtual
        returns (bytes memory)
    {
        if (entry == Entry.SetPositions || entry == Entry.RebalanceSwap) return _action(entry, args, liveRead, liveTick);
        // settle() is the prologue alone
        if (entry == Entry.Settle) return "";
        // every other entry is a moved flow: the extension runs it, or reverts BadMode for an entry it does not implement
        return _extension(entry, payer, args);
    }

    // ------------------------------------------------------------------ flows

    /// @notice Both curator actions (§ order: the spec's flow step order). `rebalanceSwap` is the action with its swap
    ///         alone; `setPositions` (`place`) adds the removal, the vault's move, the placement rules, the sizing and
    ///         the registration. After the prologue: read `s0` and the holdings before; (place) remove the old position
    ///         to idle, then the vault's own move to `moveTo` (exact output from idle, price limit `moveTo`, skipped when
    ///         spot is already there; `SwapCap` if idle cannot pay its input, `Move` unless spot ends at `moveTo`); read
    ///         the post-move spot and the live reference once (the checkpoint's successful read is reused); the common
    ///         guards on the post-move spot; (place) the width, alignment and containment of that spot and the
    ///         reference; the swap under the caps (`rebalanceSwap`'s, or the optional pre-swap); (place) the maximum
    ///         liquidity the idle supports at the post-swap spot, less a headroom; the charge over the legs `s0`, the
    ///         post-move spot and the reference; (place) `δ_after ≥ DELTA_MIN`, the range registered with the hook at
    ///         the live reference (a new clock run) and the mode set to NORMAL. `rebalanceSwap` never changes the mode.
    function _action(Entry entry, bytes memory args, bool liveRead, int24 liveTick) internal returns (bytes memory) {
        (int24 lo, int24 hi, PreSwap memory ps, uint160 moveTo, uint256 maxCharge) =
            abi.decode(args, (int24, int24, PreSwap, uint160, uint256));
        bool place = entry == Entry.SetPositions;
        // s0: the pre-move spot, read after the prologue and before any curator-controlled step
        (uint160 s0,,,) = MANAGER.getSlot0(POOL_ID);
        // [MU-007] the action is measured on the holdings it starts from (after collect-first), nothing earlier
        Holdings memory before = _holdings();
        // the vault's move: its direction and the input it paid (0 when there is none)
        bool zm;
        uint256 paidMove;
        if (place) {
            // [MU-219] the old position to idle before the move, so the move crosses only third-party liquidity
            _removeToIdle();
            // [MU-220] skipped when spot is already at moveTo (v4 rejects a price limit already reached)
            if (moveTo != 0 && moveTo != s0) {
                // [MU-221] the direction that reaches moveTo
                zm = moveTo < s0;
                BalanceDelta dm = _swapRaw(zm, _moveOut(), moveTo);
                paidMove = uint128(-(zm ? dm.amount0() : dm.amount1()));
                // [MU-222] idle pays the move's input, checked before any claim is burned
                if (paidMove > (zm ? _idle0 : _idle1)) revert SwapCap();
                _book(dm);
            }
        }
        (uint160 sSpot, int24 tickSpot,,) = MANAGER.getSlot0(POOL_ID);
        // [MU-223] the move ends at moveTo
        if (moveTo != 0 && sSpot != moveTo) revert Move();
        // [MU-108] [MU-193] [MU-210] the checkpoint's successful live read is reused; otherwise (none, or a failed one) the
        // action reads it here and reverts RefUnreadable if it cannot
        int24 ref = liveRead ? liveTick : HOOK.liveRef();
        // [MU-224] the common guards, on the post-move spot
        _guards(tickSpot, ref);
        if (place) {
            // [MU-225] the placement rules on the pre-action (post-move, pre-swap) spot
            uint8 code = Placement.check(lo, hi, tickSpot, ref, REF_BAND, W_TICKS, SPACING);
            // [MU-226] [MU-227] each rule its own error (the fence, code 1, has already passed)
            if (code == 2) revert Width();
            if (code == 3) revert Alignment();
            if (code != 0) revert NotContained();
        }
        uint160 sRef = TickMath.getSqrtPriceAtTick(ref);
        // the move's input, valued as a swap (at the reference on the holdings before the action): it shares the
        // per-action cap with the pre-swap and counts toward the epoch cap, so liquidity placed on its path cannot make
        // it a trade of vault capital beyond MAX_SWAP_ACTION (an honest move across an empty region pays ≈ nothing)
        uint256 moveFrac;
        if (paidMove > 0) {
            // [MU-243] in the token paid, at the reference
            moveFrac = ExecLedger.swapFrac(before, zm, paidMove, sRef);
            _moveCap(moveFrac);
        }
        // [MU-098] [MU-119] the swap (or the pre-swap) under the swap caps and counters, charged with the action
        if (ps.active) _cappedSwap(ps.zeroForOne, ps.amountIn, before, sRef, tickSpot, moveFrac);
        uint64 deltaAfter;
        if (place) {
            // [MU-228] L at the post-pre-swap spot
            (uint160 sNow,,,) = MANAGER.getSlot0(POOL_ID);
            uint128 L;
            (L,, deltaAfter) = ExecLedger.placementSize(_holdings(), lo, hi, sNow, sRef);
            (_lo, _hi) = (lo, hi);
            // [MU-235] a zero add reverts in the PoolManager: L = 0 reaches DeltaMin below
            if (L > 0) {
                // [MU-229] the sized L, headroom included
                _book(_modify(int256(uint256(L))));
                _L = L;
            }
        }
        // [MU-155] the s0 leg, distinct from the post-move spot
        _charge(uint8(entry), before, s0, sSpot, sRef, maxCharge);
        // [MU-060] rebalanceSwap never registers a range (no clock reset)
        // [MU-021] rebalanceSwap leaves the mode as it is
        if (place) {
            // [MU-044] [MU-062]
            if (deltaAfter < DELTA_MIN) revert DeltaMin();
            // [MU-230] [MU-231] [MU-232] a new clock run on [lo, hi) at the live reference
            HOOK.setVaultRange(lo, hi, ref);
            // [MU-233] [MU-234] NORMAL, placedByCurator
            uint8 m0 = _bank.mode;
            FeeBank.bankApply(_bank, Op.Place, "");
            _modeChanged(m0);
        }
        return "";
    }

    /// @dev The exact-input swap from idle shared by `rebalanceSwap` and the placement's pre-swap: `MAX_SWAPS`, the
    ///      per-action cap (shared with the placement's move, whose fraction is `used`) and the per-epoch cap (fractions
    ///      of NAV at the reference on the holdings before the action, rounded up, checked on the requested amount) and
    ///      idle; the price limit `FEE_TICKS + BAND_SWAP` on the adverse side
    ///      of `tickSpot` (a partial fill is charged on its result and counted by the amount filled); the settlement
    ///      through claims; the collect after it (skipped at L = 0); the counters.
    function _cappedSwap(bool z, uint128 amountIn, Holdings memory before, uint160 sRef, int24 tickSpot, uint256 used)
        internal
    {
        // [MU-015] at most MAX_SWAPS per epoch
        if (_swapCount >= MAX_SWAPS) revert MaxSwaps();
        // [MU-117] [MU-133] the caps are fractions of NAV at the reference on the holdings before the action
        uint256 frac = ExecLedger.swapFrac(before, z, amountIn, sRef);
        // [MU-022] [MU-209] the per-action and per-epoch caps, on the requested amount
        // [MU-242] the per-action budget is shared with the placement's move (`used`)
        if (frac + used > MAX_SWAP_ACTION || _epochSwapFrac + frac > MAX_SWAP_EPOCH) revert SwapCap();
        // [MU-202] from idle only
        if (amountIn > (z ? _idle0 : _idle1)) revert SwapCap();
        // [MU-020] [MU-217] the price limit on the adverse side of spot, FEE_TICKS + BAND_SWAP away
        int24 lim = z ? tickSpot - (FEE_TICKS + BAND_SWAP) : tickSpot + (FEE_TICKS + BAND_SWAP);
        BalanceDelta d = _swapRaw(z, -int256(uint256(amountIn)), TickMath.getSqrtPriceAtTick(lim));
        // [MU-218] idle moves by the amounts actually swapped
        _book(d);
        // [MU-009] the vault's own swap paid fees into its own position: collect them before NAV_after
        _collect();
        // [MU-201] the counters add the amount filled; [MU-207] and the swap
        uint256 paid = uint128(-(z ? d.amount0() : d.amount1()));
        _epochSwapFrac = uint64(_min(_epochSwapFrac + ExecLedger.swapFrac(before, z, paid, sRef), type(uint64).max));
        ++_swapCount;
    }

    /// @dev A swap by the vault (its delta, the swapper's view: negative = paid in).
    function _swapRaw(bool z, int256 amount, uint160 limit) internal returns (BalanceDelta) {
        return MANAGER.swap(_key(), SwapParams(z, amount, limit), "");
    }

    /// @dev The guards common to both actions: the fence on the spot tick at the live reference, the cooldown, and no
    ///      pending markout (the cooldown makes one due in time on a tier whose blocks keep their nominal spacing).
    function _guards(int24 tickSpot, int24 ref) internal view {
        int256 dist = int256(tickSpot) - ref;
        // [MU-206] [MU-212] both sides
        if (dist > REF_BAND || -dist > REF_BAND) revert Fence();
        uint256 la = _lastAction;
        // [MU-014] COOLDOWN blocks since the last action; none before the first
        if (la != 0 && block.number < la + COOLDOWN) revert Cooldown();
        // [MU-216] never over a pending record: the prologue has settled a due one, so one still pending is not due
        if (_tAction != 0) revert MarkoutPending();
    }

    /// @notice The charge shared by both actions: the worse of the legs `s0`, `s_spot` and the reference
    ///         (`ExecLedger.measure`), the reserve `RESERVE_RATIO·NAV_before(s_spot)`, the solvency gate and the burn
    ///         (`Op.Charge`: the charge burned, the reserve's shares encumbered), the `maxCharge` backstop on
    ///         `charge₀ + reserve`, the pending record, and the epilogue: `NAV₀` and `δ₀` re-based at the reference.
    function _charge(uint8 kind, Holdings memory before, uint160 s0, uint160 sSpot, uint160 sRef, uint256 maxCharge)
        internal
    {
        Holdings memory after_ = _holdings();
        // the gate is FeeBank's (Op.Charge), on the same burns
        (uint256 charge, uint160 pStar, uint256 burn, uint256 burnR, uint256 reserve, uint256 nav, uint64 delta) =
            ExecLedger.actionCharge(before, after_, s0, sSpot, sRef, RESERVE_RATIO, totalSupply());
        // [MU-197] [MU-198] [MU-199] the charge's burn and the reserve's shares, in that order
        (uint256 burned,, uint256 bankBurn, uint256 bondBurn) =
            FeeBank.bankApply(_bank, Op.Charge, abi.encode(burn, burnR));
        // [MU-156] [MU-192] the curator's backstop covers the charge and the reserve
        if (charge + reserve > maxCharge) revert MaxCharge();
        // [MU-256] the supply before the charge's burn
        uint256 sAct = totalSupply();
        // [MU-211] the charge burn leaves the supply
        _burn(address(this), burned);
        // [MU-257] the spot the action leaves (a placement's add never moves it)
        (uint160 sEnd,,,) = MANAGER.getSlot0(POOL_ID);
        // [MU-204] [MU-214] [MU-215] [MU-258] the pending action, settled at its markout window
        (_tAction, _pBefore, _pAfter) = (uint40(block.timestamp), before, after_);
        _pend = Markout(_u128(charge), _u128(reserve), s0, sEnd, _u128(burned), _u128(sAct));
        // [MU-205]
        _lastAction = uint40(block.number);
        // epilogue: [MU-203] NAV₀ and [MU-194] δ₀ re-based at the live reference on the new holdings
        _nav0 = _u128(nav);
        _delta0 = delta;
        // [MU-290] the record: the charge and the burns of the action
        EpochRecord storage r = _records[_epoch];
        (r.charge, r.bankBurned, r.bondBurned) =
            (_a96(r.charge, charge), _a128(r.bankBurned, bankBurn), _a128(r.bondBurned, bondBurn));
        // [MU-213]
        emit Action(kind, charge, pStar, bankBurn, bondBurn);
    }

    // ------------------------------------------------------------------ prologue

    /// @notice Step 0: the markout settlement of a pending action once its window `(tAction + blocktime, tAction +
    ///         blocktime + H_MARKOUT]` has closed (one more block). The read is guarded by `gasleft() ≥ 2·REF_GAS_CAP`;
    ///         a failed read burns the whole reserve. Its arithmetic never reverts; its only revert is the gas guard.
    function _settle() internal virtual {
        uint256 tA = _tAction;
        // [MU-188] due only once the delayed window has closed (one more block)
        if (tA == 0 || block.timestamp < tA + 2 * BLOCKTIME + H_MARKOUT) return;
        // [MU-186] the caller cannot starve the read into the fallback
        if (gasleft() < _settleGasMin()) revert RefGas();
        // [MU-151] the window opens one block after the action's block
        uint256 a = block.timestamp - tA - BLOCKTIME;
        (bool ok, int24 tickMk) = HOOK.tryWindow(uint32(a), uint32(a - H_MARKOUT));
        Bank storage b = _bank;
        uint256 extra = _pend.reserve;
        // [MU-150] a failed read burns the whole reserve
        uint256 burnS = b.reserveShares;
        if (ok) {
            // [MU-244] the pending record's holdings before and after, read in the library through storage pointers
            (extra,, burnS) = ExecLedger.settleExtra(_pBefore, _pAfter, _pend, tickMk, FEE_TICKS, FEE, burnS);
        }
        // [MU-191] the record clears (a cleared record never settles twice)
        _tAction = 0;
        // the reserve's split and its forfeit mark, read before the op clears them (S19D-03, S19D2-03)
        uint256 rb = b.reserveBond;
        uint256 nb = b.reserveShares - rb;
        bool forf = b.reserveForfeit;
        // [MU-187] the released non-bond reserve enters the current epoch's cohort
        // [MU-190] the bank's clock is the block number (the drip it may restart runs in blocks)
        (uint256 burned,, uint256 relBond, uint256 relNb) =
            FeeBank.bankApply(b, Op.Settle, abi.encode(burnS, uint32(block.number / EPOCH), uint40(block.number)));
        if (burned > 0) _burn(address(this), burned);
        // [MU-291] the record: the settlement's value and burns, and a forfeited reserve's non-bond part
        EpochRecord storage r = _records[_epoch];
        r.charge = _a96(r.charge, extra);
        r.bankBurned = _a128(r.bankBurned, nb - relNb);
        r.bondBurned = _a128(r.bondBurned, rb - relBond);
        if (forf) r.forfeited = _a128(r.forfeited, relNb);
        emit Settled(_pend.charge0, extra, tickMk, !ok);
    }

    /// @dev Snapshot, collect first, checkpoint (virtual: a seam for the test harness's positive controls). The markout settlement (step 0) and the checkpoint body are added with
    ///      the curator actions and the bank; the order below is the binding one.
    function _prologue() internal virtual returns (Obs memory o) {
        // [MU-027] the hook's span is closed in this block before anything reads or registers a range
        o = HOOK.snapshotIfNewBlock();
        // [MU-008] collect first: v4 pays all accrued fees on any modify, so sweep them into idle before pricing
        _collect();
    }

    /// @dev Sweeps the position's accrued fees into idle (as claims); skipped at L = 0, where a zero-delta modify
    ///      reverts.
    function _collect() internal {
        // [MU-200]
        if (_L > 0) _book(_modify(0));
    }

    /// @dev The position to idle (value-neutral at the spot it is removed at; its fees were collected first); skipped
    ///      at L = 0.
    function _removeToIdle() internal {
        uint128 l = _L;
        if (l > 0) {
            // [MU-310] all of it: the vault holds one position (I-13)
            _book(_modify(-int256(uint256(l))));
            _L = 0;
        }
    }

    // ------------------------------------------------------------------ checkpoint

    /// @notice The breach-first checkpoint: (1) the vest gate, (2) the forfeit drip, (3) pending breaches, (4) the
    ///         segment (base accrual is added with the curator actions; until then no base exists), (5) vesting,
    ///         (6) the epoch roll (counters; the epoch records are added with the records). Returns the live reference
    ///         read, if the gate made one and it succeeded, so an action in the same call can reuse it.
    function _checkpoint(Obs memory o) internal returns (bool liveRead, int24 liveTick) {
        if (totalSupply() == 0) return (false, 0);
        Bank storage b = _bank;
        uint40 now_ = uint40(block.number);
        uint32 ne = o.epoch;
        int24 lo = _lo;
        int24 hi = _hi;
        // the record of the stored epoch: the segment closed here opened in it (spec §3.9)
        EpochRecord storage r = _records[_epoch];
        uint8 fl;
        // [MU-292] [MU-293]
        if (o.gapSinceVerify) fl = F_UNVERIFIED;
        if (!o.refFresh) fl |= F_REF_STALE;
        // (1) the vest gate, evaluated once: the cache legs, then the live leg only when something is vestable
        bool vestable = _vestable(b, ne);
        bool gateOpen;
        // [MU-069] [MU-093] [MU-106] [MU-164] [MU-165] a fresh, gap-free cached reference inside the registered range
        // [MU-287] (the gap leg alone, after a return in range: U-vest-park)
        if (o.refFresh && !o.emptyRange && !o.gapSinceVerify && lo <= o.refTick && o.refTick < hi && vestable) {
            (liveRead, liveTick) = _tryRef();
            // [MU-070] and the live reference inside the range (a failed read never opens the gate)
            gateOpen = liveRead && lo <= liveTick && liveTick < hi;
        }
        // (2) the forfeit drip up to now
        // [MU-168]
        if (b.forfeitPool > 0) _burnBank(Op.Drip, abi.encode(now_));
        // (3) pending breaches, before any accrual or vesting
        uint32 n = o.pendingBreaches;
        // [MU-049] every checkpoint processes pending breaches
        if (n > 0) {
            // the cut span [τ_v0, tauVAtLastPending): exposure and gross base in the record, its base forfeited
            // (S19D-01); eligibility read before the op (the removal breach clears placedByCurator, S19D2-02)
            uint64 cutTau = o.tauVAtLastPending > _tauV0 ? o.tauVAtLastPending - _tauV0 : 0;
            // [MU-294]
            r.lTau = _a192(r.lTau, uint256(_L) * cutTau);
            uint256 cut = b.placedByCurator && b.mode != Ladder.EXITED ? _baseAmount(cutTau) : 0;
            r.base = _a96(r.base, cut);
            // [MU-295] the forfeited base value: the cut span and the deferred base
            r.forfeitValue = _a96(r.forfeitValue, cut + _baseOwed);
            // [MU-080] [MU-097] deferred base is forfeited by a breach
            _baseOwed = 0;
            uint8 m0 = b.mode;
            // [MU-123] every pending breach is counted and acknowledged
            uint256 forfeited = _burnBank(Op.Breach, abi.encode(n, now_));
            r.forfeited = _a128(r.forfeited, forfeited);
            fl |= F_BREACH;
            _modeChanged(m0);
            // [MU-166] the hook's pending count is reduced by what was processed
            HOOK.ackBreaches(n);
            emit BreachProcessed(n, o.lastPendingBlock, forfeited);
            // [MU-050] the breach cutoff, then the segment below closes at now
            _tauV0 = o.tauVAtLastPending;
            _segStart = o.lastPendingBlock;
        }
        // (4) base accrual for the segment [segStart, now) (spec §3.5): only on a curator-placed range in NORMAL or
        // BREACHED; the pre-breach part was cut by τ_v0 := τ_v at the last pending breach above
        // [MU-034]
        uint256 base = b.placedByCurator && b.mode != Ladder.EXITED ? _baseAmount(o.tauV - _tauV0) : 0;
        uint256 amount = base + _baseOwed;
        // [MU-296] the record: the segment's gross base and its exposure L·Δτ_v
        r.base = _a96(r.base, base);
        r.lTau = _a192(r.lTau, uint256(_L) * (o.tauV - _tauV0));
        if (amount > 0) {
            // the price: the cached reference if fresh, else a live read (none if it fails: the amount is deferred)
            bool priced = o.refFresh;
            int24 pt = o.refTick;
            if (!priced) {
                // [MU-272] no fresh cache: a fail-soft read (a failed one defers the base; a flow never reverts on the reference)
                (priced, pt) = _tryRef();
                if (priced) (liveRead, liveTick) = (true, pt);
            }
            bool minted;
            if (priced) {
                // [MU-109] NAV' at the reference, on the holdings after collect-first
                (uint256 navp,) = ExecLedger.value(_holdings(), TickMath.getSqrtPriceAtTick(pt));
                // [MU-271] strict: at navp == amount the share count is unbounded (a mulDiv by zero would revert)
                if (navp > amount) {
                    uint256 shares = FullMath.mulDiv(amount, totalSupply(), navp - amount);
                    FeeBank.bankApply(b, Op.Accrue, abi.encode(ne, shares, gateOpen));
                    _mint(address(this), shares);
                    // [MU-270] a mint inside a pending action's markout window enters its supply (S15D-07)
                    if (_tAction != 0) _pend.sAct = _u128(uint256(_pend.sAct) + shares);
                    // [MU-083]
                    _baseOwed = 0;
                    minted = true;
                    r.minted = _a128(r.minted, shares);
                }
            }
            if (!minted) {
                _baseOwed = _u128(amount);
                fl |= F_DEFERRED;
                emit CheckpointDeferred();
            }
        }
        _tauV0 = o.tauV;
        _segStart = now_;
        r.flags |= fl;
        emit Checkpoint(o.tauV, o.tauB, _nav0, _delta0, base, _L, lo, hi);
        // (5) [MU-030] vest after breach processing: vested with the gate open, else held in maturedPending
        // [MU-171] the gate decides
        if (vestable) FeeBank.bankApply(b, Op.VestTime, abi.encode(ne, gateOpen));
        // (6) the epoch roll: the extension closes the record(s), opens the new one and advances the epoch
        if (ne > _epoch) {
            _delegate(abi.encodeCall(ITouchstoneVaultExtension.roll, (o, ne)));
            // [MU-132] the swap counters restart every epoch
            _epochSwapFrac = 0;
            _swapCount = 0;
        }
    }

    /// @dev The base amount for `dtau` in-range blocks: `mulDiv(mulDiv(m_perBlock, NAV₀, WAD), δ₀·dtau, WAD)`, both floor.
    function _baseAmount(uint64 dtau) internal view returns (uint256) {
        return FullMath.mulDiv(FullMath.mulDiv(M_PER_BLOCK, _nav0, WAD), uint256(_delta0) * dtau, WAD);
    }

    /// @dev Some cohort is due (`vestBlock = (e + 1 + V)·EPOCH ≤ now`) or `maturedPending > 0`.
    function _vestable(Bank storage b, uint32 ne) internal view returns (bool) {
        // [MU-078] maturedPending alone makes the live leg due
        if (b.maturedPending > 0) return true;
        for (uint256 k; k < RING; ++k) {
            // [MU-169] due at (e + 1 + V)·EPOCH
            if (b.cohorts[k].shares > 0 && uint256(ne) >= uint256(b.cohorts[k].epoch) + 1 + V) return true;
        }
        return false;
    }

    /// @dev The exact output the vault's move requests: `type(int128).max`, so the swap stops at its price limit (a seam
    ///      for the test harness, which reaches a miss with a finite amount).
    function _moveOut() internal view virtual returns (int256) {
        return type(int128).max;
    }

    /// @dev The settlement's gas guard, `2·REF_GAS_CAP` (a seam for the test harness's control).
    function _settleGasMin() internal view virtual returns (uint256) {
        return 2 * REF_GAS_CAP;
    }

    /// @dev The vault's move held to the per-action cap (shared with the pre-swap) and the epoch cap, and counted in the
    ///      epoch counter (a seam for the test harness's positive controls).
    function _moveCap(uint256 moveFrac) internal virtual {
        // [MU-239] [MU-240]
        if (moveFrac > MAX_SWAP_ACTION || _epochSwapFrac + moveFrac > MAX_SWAP_EPOCH) revert SwapCap();
        // [MU-241]
        _epochSwapFrac += uint64(moveFrac);
    }

    /// @dev The live reference read of the vest gate (a seam for the scripted test harness).
    function _tryRef() internal virtual returns (bool ok, int24 tick) {
        // [MU-076] the gate reads the live reference, never the cache
        return HOOK.tryRef();
    }
}
