// SPDX-License-Identifier: MIT
pragma solidity 0.8.26;

import {BaseHook} from "@openzeppelin/uniswap-hooks/base/BaseHook.sol";
import {IPoolManager} from "@uniswap/v4-core/src/interfaces/IPoolManager.sol";
import {IHooks} from "@uniswap/v4-core/src/interfaces/IHooks.sol";
import {Hooks} from "@uniswap/v4-core/src/libraries/Hooks.sol";
import {StateLibrary} from "@uniswap/v4-core/src/libraries/StateLibrary.sol";
import {TickMath} from "@uniswap/v4-core/src/libraries/TickMath.sol";
import {FullMath} from "@uniswap/v4-core/src/libraries/FullMath.sol";
import {FixedPoint128} from "@uniswap/v4-core/src/libraries/FixedPoint128.sol";
import {Extsload} from "@uniswap/v4-core/src/Extsload.sol";
import {PoolKey} from "@uniswap/v4-core/src/types/PoolKey.sol";
import {PoolId} from "@uniswap/v4-core/src/types/PoolId.sol";
import {Currency} from "@uniswap/v4-core/src/types/Currency.sol";
import {BalanceDelta} from "@uniswap/v4-core/src/types/BalanceDelta.sol";
import {BeforeSwapDelta} from "@uniswap/v4-core/src/types/BeforeSwapDelta.sol";
import {SwapParams, ModifyLiquidityParams} from "@uniswap/v4-core/src/types/PoolOperation.sol";
import {LiquidityAmounts} from "@uniswap/v4-periphery/src/libraries/LiquidityAmounts.sol";
import {ITouchstoneHook} from "./interfaces/ITouchstoneHook.sol";
import {IUniswapV3PoolOracle} from "./interfaces/IUniswapV3PoolOracle.sol";
import {HookArgs} from "./interfaces/ITouchstoneFactory.sol";
import {Tier} from "./types/Tier.sol";
import {Obs} from "./types/Obs.sol";
import {MedianOracle} from "./libraries/MedianOracle.sol";

/// @title TouchstoneHook
/// @notice The hook of one Touchstone pool. It keeps:
///         - block-start snapshots of the pool tick in a ring of 32, and their median of 9 (records only);
///         - a cache of the reference price: the TWAP of an immutable deep Uniswap v3 pool, sampled on timestamp
///           anchors `k = ts / GRID_SEC` (the TWAP ending at `k * GRID_SEC`), at most once per anchor;
///         - the in-range block counters of the vault (tauV) and of the witness (tauB), and the breach clock
///           (`outBlocks`) with its latch, all credited only for time the cached reference was observed fresh;
///         - the frozen per-grid records of the last 8 grid boundaries;
///         - the witness: a passive-mandate position owned by the hook, rolled once per epoch (disclosure only).
///         The reference is the only external input. Snapshots, swaps and every vault exit path are fail-soft with
///         respect to it; only `liveRef` reverts. The pool is self-initialized in the constructor; `initialize` and
///         `donate` by anyone else revert.
contract TouchstoneHook is BaseHook, Extsload, ITouchstoneHook {
    using StateLibrary for IPoolManager;

    // ------------------------------------------------------------------ immutables
    address public immutable VAULT;
    PoolId public immutable POOL_ID;
    // [MU-128] the reference address is an immutable
    address public immutable REF_POOL;
    uint32 internal immutable REF_WINDOW;
    uint256 internal immutable REF_GAS_CAP;
    uint256 internal immutable REFRESH_GAS_MIN;
    uint256 internal immutable GRID_SEC;
    uint256 internal immutable GRID;
    uint256 internal immutable EPOCH;
    uint256 internal immutable CURE_BLOCKS;
    uint256 internal immutable RESET_RUN;
    int24 internal immutable W_TICKS;
    uint128 internal immutable L_W_TARGET;
    int24 internal immutable TICK_SPACING;
    uint24 internal immutable FEE;
    Currency internal immutable CURRENCY0;
    Currency internal immutable CURRENCY1;

    // ------------------------------------------------------------------ storage
    // Packed so that a swap's snapshot touches three slots (the hot slot, the counters, one ring slot).
    // hot slot: the snapshot's own state and the reference cache
    uint40 internal _lastSnapBlock;
    uint8 internal _head;
    uint8 internal _count;
    bool internal _armed;
    bool internal _emptyRange = true;
    int24 internal _vaultLo;
    int24 internal _vaultHi;
    int24 internal _refTick;
    uint32 internal _refGrid;
    uint40 internal _refCachedAt;
    uint8 internal _resetStreak;
    bool internal _gapSinceVerify;

    // counters and the witness range they are credited against
    uint64 internal _tauV;
    uint64 internal _tauB;
    uint64 internal _outBlocks;
    int24 internal _witLo;
    int24 internal _witHi;

    // the latch and the epoch's reference
    uint32 internal _breachSeq;
    uint32 internal _pendingBreaches;
    uint40 internal _lastPendingBlock;
    uint64 internal _tauVAtLastPending;
    int24 internal _epochStartRef;
    uint32 internal _epochStartRefEpoch;
    bool internal _witnessFallback;

    // the witness position
    uint32 internal _witEpoch;
    uint128 internal _witL;

    uint128 internal _witnessReserve0;
    uint128 internal _witnessReserve1;

    uint64[32] internal _ring;

    struct Frozen {
        uint32 gridIndex;
        int24 med9;
        uint64 tauV;
        uint64 tauB;
    }

    Frozen[8] internal _frozen;

    uint256 internal constant L_REF = 1e18;

    modifier onlyVault() {
        // [MU-025] range registration, arming, acknowledgement and the witness are vault-only
        if (msg.sender != VAULT) revert OnlyVault();
        _;
    }

    /// @param a PoolManager, vault (precomputed CREATE2 address), pool key fields, seed price and tier.
    constructor(HookArgs memory a) BaseHook(IPoolManager(a.poolManager)) {
        Tier memory t = a.tier;
        if (t.REF_WINDOW == 0 || t.GRID == 0 || t.EPOCH % t.GRID != 0) revert BadTier();
        // [MU-306] [MU-307] [MU-308] the forfeit drips over exactly one epoch (F-047): a shorter drip lets a sniper who
        // deposits at the breach take its share at its exit, a longer one withholds forfeited pay from leaving depositors
        if (t.EPOCH == 0 || t.FORFEIT_DRIP_BLOCKS != t.EPOCH) revert BadTier();
        // [MU-141] the tier's clock invariants hold on chain, not only in the deploy script: whole-second blocks on
        // the grid and a reset that needs at least two consecutive in-range anchors
        // [MU-143] whole-second blocks on the grid
        if (t.GRID_SEC == 0 || t.GRID_SEC % t.GRID != 0 || t.RESET_RUN < 2) revert BadTier();
        // [MU-142] the cure window covers the edge wait, two grids of freshness, the reference window and a grid
        if (
            uint256(t.CURE_BLOCKS)
                < uint256(t.EDGE_DELAY) + 3 * uint256(t.GRID) + t.REF_WINDOW / (t.GRID_SEC / t.GRID)
        ) revert BadTier();
        // [MU-144] the witness range [c - W/2, c + W/2) is W wide and aligned to the pool's tick spacing
        if (t.W_TICKS <= 0 || t.W_TICKS % (2 * a.tickSpacing) != 0) revert BadTier();
        // [MU-259] a static LP fee: the vault's settlement prices the arbitrage fee at the key's fee (the dynamic-fee
        // flag, and any fee of 100% or more, is rejected)
        if (a.fee >= 1_000_000) revert BadTier();
        IUniswapV3PoolOracle r = IUniswapV3PoolOracle(t.REF_POOL);
        // [MU-114] the reference prices the same pair in the same orientation
        if (r.token0() != Currency.unwrap(a.currency0) || r.token1() != Currency.unwrap(a.currency1)) revert BadRef();
        (,,, uint16 card,,,) = r.slot0();
        // [MU-104] the reference keeps enough observations for the anchored window
        if (card < t.MIN_CARD) revert BadRef();
        // The reference must already hold GRID_SEC + REF_WINDOW of history (reverts "OLD" otherwise).
        uint32[] memory ago = new uint32[](2);
        ago[0] = t.GRID_SEC + t.REF_WINDOW;
        r.observe(ago);

        VAULT = a.vault;
        REF_POOL = t.REF_POOL;
        REF_WINDOW = t.REF_WINDOW;
        REF_GAS_CAP = t.REF_GAS_CAP;
        REFRESH_GAS_MIN = t.REFRESH_GAS_MIN;
        GRID_SEC = t.GRID_SEC;
        GRID = t.GRID;
        EPOCH = t.EPOCH;
        CURE_BLOCKS = t.CURE_BLOCKS;
        RESET_RUN = t.RESET_RUN;
        W_TICKS = t.W_TICKS;
        L_W_TARGET = t.L_W_TARGET;
        TICK_SPACING = a.tickSpacing;
        FEE = a.fee;
        CURRENCY0 = a.currency0;
        CURRENCY1 = a.currency1;
        PoolKey memory key = PoolKey(a.currency0, a.currency1, a.fee, a.tickSpacing, IHooks(address(this)));
        POOL_ID = key.toId();
        // The PoolManager skips the hook's callbacks on the hook's own call.
        IPoolManager(a.poolManager).initialize(key, a.seedSqrtPriceX96);
    }

    function getHookPermissions() public pure override returns (Hooks.Permissions memory p) {
        p.beforeInitialize = true;
        p.beforeSwap = true;
        p.beforeDonate = true;
    }

    // ------------------------------------------------------------------ callbacks

    function _beforeInitialize(address, PoolKey calldata, uint160) internal pure override returns (bytes4) {
        // [MU-026] initialization by anyone but the hook's own constructor reverts
        revert HookNotImplemented();
    }

    function _beforeDonate(address, PoolKey calldata, uint256, uint256, bytes calldata)
        internal
        pure
        override
        returns (bytes4)
    {
        // [MU-138] donations to the pool revert
        revert HookNotImplemented();
    }

    function _beforeSwap(address, PoolKey calldata key, SwapParams calldata, bytes calldata)
        internal
        override
        returns (bytes4, BeforeSwapDelta, uint24)
    {
        // [MU-024] pool-id check
        if (PoolId.unwrap(key.toId()) != PoolId.unwrap(POOL_ID)) revert WrongPool();
        _snapshot();
        return (this.beforeSwap.selector, BeforeSwapDelta.wrap(0), 0);
    }

    // ------------------------------------------------------------------ open entry points

    function snapshotIfNewBlock() external returns (Obs memory) {
        _snapshot();
        return obs();
    }

    function refreshRef() external {
        // [MU-112] a standalone refresh closes the open span with the old cache first
        _snapshot();
        _refresh();
    }

    // ------------------------------------------------------------------ views

    function obs() public view returns (Obs memory o) {
        (, int24 tick) = _slot0();
        o.med9 = MedianOracle.med9(_ring, _head, _count);
        o.tickSpot = tick;
        o.refTick = _refTick;
        o.refCachedAt = _refCachedAt;
        o.refFresh = block.number < uint256(_refCachedAt) + 2 * GRID;
        o.resetStreak = _resetStreak;
        o.gapSinceVerify = _gapSinceVerify;
        o.emptyRange = _emptyRange;
        o.tauV = _tauV;
        o.tauB = _tauB;
        o.outBlocks = _outBlocks;
        o.breachSeq = _breachSeq;
        o.pendingBreaches = _pendingBreaches;
        o.lastPendingBlock = _lastPendingBlock;
        o.tauVAtLastPending = _tauVAtLastPending;
        o.gridIndex = uint32(block.number / GRID);
        o.epoch = uint32(block.number / EPOCH);
    }

    function liveRef() external view returns (int24 tick) {
        bool ok;
        (ok, tick) = _readRef(0);
        // [MU-127] an unreadable reference fails closed for curator actions
        if (!ok) revert RefUnreadable();
    }

    function tryRef() external view returns (bool ok, int24 tick) {
        return _readRef(0);
    }

    function tryWindow(uint32 a, uint32 b) external view returns (bool ok, int24 tick) {
        // [MU-139] the markout read covers exactly (now - a, now - b]
        return _window(a, b);
    }

    function epochStartRef() external view returns (int24) {
        return _epochStartRef;
    }

    function frozen(uint32 gridIndex) external view returns (int24 med9, uint64 tauV, uint64 tauB, bool exists) {
        Frozen memory f = _frozen[gridIndex % 8];
        exists = gridIndex != 0 && f.gridIndex == gridIndex;
        if (exists) (med9, tauV, tauB) = (f.med9, f.tauV, f.tauB);
    }

    // ------------------------------------------------------------------ vault-only

    /// @notice Registers the vault's range and starts a new clock run: the cache takes the live reference the
    ///         placement was checked against, the gap flag and the streak are cleared and `outBlocks` restarts.
    ///         Pending breaches are kept. Every crossing up to now was latched by the prologue snapshot.
    function setVaultRange(int24 lo, int24 hi, int24 liveTick) external onlyVault {
        _vaultLo = lo;
        _vaultHi = hi;
        _emptyRange = false;
        // [MU-099] the cache takes the live tick
        _writeCache(liveTick, _anchor());
        // [MU-081] registration clears the gap
        _gapSinceVerify = false;
        emit GapCleared(0);
        // [MU-043] [MU-090] registration starts a run; pending breaches are kept
        _resetStreak = 0;
        _outBlocks = 0;
        emit RangeRegistered(lo, hi, liveTick);
    }

    /// @notice Arms the clock with the EMPTY range and refreshes the cache, which must succeed in this block.
    function armAtSeed() external onlyVault {
        if (_armed) revert Armed();
        // [MU-120] the clock is armed at seed
        _armed = true;
        _lastSnapBlock = uint40(block.number);
        _refresh();
        // [MU-275] the seed requires a successful refresh in its own block
        if (_refCachedAt != block.number) revert RefUnreadable();
    }

    function ackBreaches(uint32 n) external onlyVault {
        uint32 p = _pendingBreaches;
        // [MU-091] subtracts the processed count (saturating), never clears
        _pendingBreaches = n >= p ? 0 : p - n;
    }

    /// @notice Records witness funding: the vault has transferred `a0`/`a1` of ERC-6909 claims to the hook.
    function fundWitness(uint256 a0, uint256 a1) external onlyVault {
        if (a0 > type(uint128).max || a1 > type(uint128).max) revert Overflow();
        _witnessReserve0 += uint128(a0);
        _witnessReserve1 += uint128(a1);
    }

    /// @notice Rolls the witness to the epoch's passive-mandate range, centred on `epochStartRef`. Called inside the
    ///         vault's `unlock`; ignores `epoch <= witEpoch`; never reverts (a failed roll leaves the witness as it
    ///         was and returns `lw = 0`, which the vault records as `witnessMissing`).
    /// @return fg0 the token0 fees a 1e18-liquidity position on the previous witness range earned since it opened
    /// @return fg1 the same for token1
    /// @return lw the new witness liquidity
    /// @return fallbackUsed the epoch had no successful refresh yet (the previous epochStartRef is used)
    /// @return status 0 the call was ignored (`epoch <= witEpoch`), 1 the witness rolled, 2 the roll failed (the vault's
    ///         record takes the last non-ignored roll, spec §3.9)
    function rollWitness(uint32 epoch)
        external
        onlyVault
        returns (uint256 fg0, uint256 fg1, uint128 lw, bool fallbackUsed, uint8 status)
    {
        if (epoch <= _witEpoch) return (0, 0, 0, false, 0);
        fallbackUsed = _epochStartRefEpoch != epoch;
        // [MU-131] the roll is fail-soft
        status = 2;
        try this.witnessStep(epoch) returns (uint256 a, uint256 b, uint128 l) {
            (fg0, fg1, lw) = (a, b, l);
            // the flag describes the live witness: a failed roll leaves both unchanged
            _witnessFallback = fallbackUsed;
            // [MU-297]
            status = 1;
        } catch {}
        emit WitnessRolled(epoch, fg0, fg1, lw);
    }

    /// @notice The witness roll body; callable by the hook itself only (so that `rollWitness` can catch a failure).
    function witnessStep(uint32 epoch) external returns (uint256 fg0, uint256 fg1, uint128 lw) {
        if (msg.sender != address(this)) revert OnlySelf();
        PoolKey memory key = PoolKey(CURRENCY0, CURRENCY1, FEE, TICK_SPACING, IHooks(address(this)));
        uint128 oldL = _witL;
        if (oldL != 0) {
            int24 lo = _witLo;
            int24 hi = _witHi;
            bytes32 salt = _witSalt(_witEpoch);
            (, uint256 last0, uint256 last1) = poolManager.getPositionInfo(POOL_ID, address(this), lo, hi, salt);
            // removing the liquidity updates the position's fee growth to the pool's current value
            _modify(key, ModifyLiquidityParams(lo, hi, -int256(uint256(oldL)), salt));
            (, uint256 now0, uint256 now1) = poolManager.getPositionInfo(POOL_ID, address(this), lo, hi, salt);
            unchecked {
                fg0 = FullMath.mulDiv(now0 - last0, L_REF, FixedPoint128.Q128);
                fg1 = FullMath.mulDiv(now1 - last1, L_REF, FixedPoint128.Q128);
            }
        }
        int24 c = _epochStartRef;
        int24 base = c / TICK_SPACING * TICK_SPACING;
        if (c < 0 && c % TICK_SPACING != 0) base -= TICK_SPACING;
        int24 nlo = base - W_TICKS / 2;
        int24 nhi = base + W_TICKS / 2;
        (uint160 sqrtP,) = _slot0();
        uint128 r0 = _witnessReserve0;
        uint128 r1 = _witnessReserve1;
        // one unit of headroom per token for the rounding-up of the add amounts
        lw = LiquidityAmounts.getLiquidityForAmounts(
            sqrtP,
            TickMath.getSqrtPriceAtTick(nlo),
            TickMath.getSqrtPriceAtTick(nhi),
            r0 == 0 ? 0 : r0 - 1,
            r1 == 0 ? 0 : r1 - 1
        );
        if (lw > L_W_TARGET) lw = L_W_TARGET;
        if (lw != 0) {
            // [MU-065] the salt is per epoch; [MU-309] the position is added on the range it records
            _modify(key, ModifyLiquidityParams(nlo, nhi, int256(uint256(lw)), _witSalt(epoch)));
        }
        (_witLo, _witHi, _witEpoch, _witL) = (nlo, nhi, epoch, lw);
    }

    // ------------------------------------------------------------------ the snapshot procedure

    /// @dev Runs on the first touch of each block. Before arming it only records the ring entry.
    function _snapshot() internal {
        uint256 now_ = block.number;
        uint256 last = _lastSnapBlock;
        if (last == now_ && _count != 0) return;
        (uint160 sqrtP, int24 tick) = _slot0();
        if (!_armed) {
            _insert(now_, tick);
            _lastSnapBlock = uint40(now_);
            return;
        }
        // 1. Read: the TWAP ending at the anchor, if this anchor is not cached yet. No state is written.
        uint32 k = _anchor();
        bool read = k > _refGrid;
        bool ok;
        int24 t;
        // [MU-113] the snapshot-path read is fail-soft
        if (read) (ok, t) = _readAnchor(k);

        // 2-3. Close the span [last, now) at the block-start price, crediting only the cache's freshness window.
        int24 lo = _vaultLo;
        int24 hi = _vaultHi;
        int24 ref = _refTick;
        uint256 freshEnd = uint256(_refCachedAt) + 2 * GRID;
        // [MU-041] [MU-085] credit stops at the end of the cache's freshness
        uint256 end = now_ < freshEnd ? now_ : freshEnd;
        uint256 trunc = end > last ? end - last : 0;
        uint64 tauV0 = _tauV;
        uint64 tauB0 = _tauB;
        uint64 oldOut = _outBlocks;
        uint64 newOut = oldOut;
        // [MU-040] [MU-077] the clock runs on the cached reference; EMPTY (lo = hi = 0) is out of range
        bool refIn = lo <= ref && ref < hi;
        // [MU-092] [MU-035] tauV needs the pool price strictly inside the range and the cached reference in range.
        // The pool keeps sqrtAt(tick) <= sqrtP <= sqrtAt(tick + 1), so only a tick on an edge needs the sqrt test.
        // [MU-140] the upper edge: a price stopped exactly at sqrtAt(hi) from above has tick hi - 1
        bool creditV = refIn && !_emptyRange && (tick > lo || (tick == lo && sqrtP > TickMath.getSqrtPriceAtTick(lo)))
            && (tick < hi - 1 || (tick == hi - 1 && sqrtP < TickMath.getSqrtPriceAtTick(hi)));
        bool creditB = _witLo <= tick && tick < _witHi;
        if (creditV) _tauV = _satAdd(tauV0, trunc);
        if (creditB) _tauB = _satAdd(tauB0, trunc);
        if (!refIn) {
            newOut = _satAdd(oldOut, trunc);
            _outBlocks = newOut;
        }
        // [MU-086] [MU-079] a span longer than the freshness window sets the gap
        if (now_ > freshEnd) {
            _gapSinceVerify = true;
            emit GapSet(uint40(now_));
        }
        // Freeze the last (at most 8) grid boundaries B with last < B <= now, at the pre-insert median.
        {
            uint256 bMax = now_ / GRID * GRID;
            uint256 bMin = last / GRID * GRID + GRID;
            if (bMax >= bMin) {
                if (bMax - bMin > 7 * GRID) bMin = bMax - 7 * GRID;
                int24 m = MedianOracle.med9(_ring, _head, _count);
                for (uint256 b = bMin; b <= bMax; b += GRID) {
                    // [MU-057] tau at B is interpolated within the credited part of the span
                    uint256 e = b < freshEnd ? b : freshEnd;
                    uint256 cr = e > last ? e - last : 0;
                    if (cr > trunc) cr = trunc;
                    _frozen[(b / GRID) % 8] = Frozen(
                        uint32(b / GRID),
                        m,
                        creditV ? _satAdd(tauV0, cr) : tauV0,
                        creditB ? _satAdd(tauB0, cr) : tauB0
                    );
                }
            }
        }
        // 4. Insert. [MU-042] the span above is credited with the pre-insert state
        _insert(now_, tick);
        // 5. Commit. [MU-058] the commit follows the close of the span
        if (read) _commit(ok, t, k);
        // 6. Latch the crossings of step 3.
        _latch(oldOut, newOut, last);
        _lastSnapBlock = uint40(now_);
    }

    /// @dev Steps 1 and 5 only; the only writer of the cache besides `setVaultRange`. A gas-starved call reverts.
    function _refresh() internal {
        // [MU-100] a refresh never runs gas-starved
        if (gasleft() < REFRESH_GAS_MIN) revert RefGas();
        uint32 k = _anchor();
        // [MU-110] at most one sample per anchor
        if (k > _refGrid) {
            (bool ok, int24 t) = _readAnchor(k);
            _commit(ok, t, k);
        }
    }

    /// @dev Step 5: commit a successful anchor read and advance the reset streak; a failed read changes nothing.
    function _commit(bool ok, int24 t, uint32 k) internal {
        if (!ok) {
            emit RefReadFailed(k);
            return;
        }
        uint256 streak;
        // [MU-087] an in-range sample alone does not clear the gap
        if (_vaultLo <= t && t < _vaultHi) {
            // [MU-116] the streak counts consecutive anchors only
            streak = _refGrid + 1 == k ? _resetStreak + 1 : 1;
            if (streak > RESET_RUN) streak = RESET_RUN;
        }
        // [MU-115] an out-of-range sample zeroes the streak
        _writeCache(t, k);
        emit RefRefreshed(k, t);
        // [MU-061] a reset needs RESET_RUN consecutive in-range anchors
        if (streak >= RESET_RUN) {
            _outBlocks = 0;
            // [MU-068] the reset clears the gap
            _gapSinceVerify = false;
            emit GapCleared(1);
            streak = 0;
        }
        _resetStreak = uint8(streak);
    }

    function _writeCache(int24 t, uint32 k) internal {
        uint32 e = uint32(block.number / EPOCH);
        // the epoch's first cache write sets epochStartRef
        if (_epochStartRefEpoch != e || _refCachedAt == 0) (_epochStartRef, _epochStartRefEpoch) = (t, e);
        (_refTick, _refGrid, _refCachedAt) = (t, k, uint40(block.number));
    }

    /// @dev Step 6: one breach per multiple of CURE_BLOCKS that outBlocks crossed in step 3 (at most 3 per call).
    function _latch(uint256 oldOut, uint256 newOut, uint256 last) internal {
        uint256 c = CURE_BLOCKS;
        uint256 n;
        // [MU-107] [MU-059] [MU-039] one latch per CURE_BLOCKS crossed, at the crossing
        for (uint256 j = oldOut / c + 1; j * c <= newOut && n < 3; ++j) {
            uint32 seq = _breachSeq == type(uint32).max ? type(uint32).max : _breachSeq + 1;
            _breachSeq = seq;
            if (_pendingBreaches != type(uint32).max) ++_pendingBreaches;
            uint40 at = uint40(last + (j * c - oldOut));
            // [MU-052] [MU-121] the forfeit cutoff is the LAST pending crossing, recorded at every latch
            _lastPendingBlock = at;
            _tauVAtLastPending = _tauV;
            emit BreachLatched(seq, at);
            ++n;
        }
    }

    function _insert(uint256 blockNumber, int24 tick) internal {
        uint256 h = _head;
        _ring[h] = MedianOracle.pack(blockNumber, tick);
        _head = uint8((h + 1) % MedianOracle.N_SNAP);
        if (_count < MedianOracle.N_SNAP) ++_count;
    }

    // ------------------------------------------------------------------ the reference reads

    /// @dev The anchor index of the current timestamp.
    function _anchor() internal view returns (uint32) {
        // [MU-105] samples are gated on the timestamp grid
        return uint32(block.timestamp / GRID_SEC);
    }

    /// @dev The TWAP ending at the anchor's boundary `k * GRID_SEC`.
    function _readAnchor(uint32 k) internal view returns (bool, int24) {
        // [MU-111] boundary-anchored
        return _readRef(block.timestamp - uint256(k) * GRID_SEC);
    }

    /// @dev The TWAP of the reference over [now - secondsAgo - REF_WINDOW, now - secondsAgo].
    function _readRef(uint256 secondsAgo) internal view returns (bool, int24) {
        // [MU-126] the window is REF_WINDOW seconds of the reference's oracle
        return _window(uint32(secondsAgo) + REF_WINDOW, uint32(secondsAgo));
    }

    /// @dev `observe([a, b])` with a gas cap and a fixed return-data size; never reverts. The tick is
    ///      floor((cum_b - cum_a) / (a - b)) rounded toward negative infinity, clamped to the tick range.
    function _window(uint32 a, uint32 b) internal view returns (bool ok, int24 tick) {
        if (a <= b) return (false, 0);
        address pool = REF_POOL;
        uint256 cap = REF_GAS_CAP;
        int256 d;
        assembly ("memory-safe") {
            let p := mload(0x40)
            mstore(p, shl(224, 0x883bdbfd)) // observe(uint32[])
            mstore(add(p, 0x04), 0x20)
            mstore(add(p, 0x24), 2)
            mstore(add(p, 0x44), a)
            mstore(add(p, 0x64), b)
            // [MU-103] at most 0x100 bytes of return data are copied
            let s := staticcall(cap, pool, p, 0x84, p, 0x100)
            // [MU-102] exactly two cumulatives and two seconds-per-liquidity values
            ok := and(s, eq(returndatasize(), 0x100))
            if ok { d := sub(signextend(6, mload(add(p, 0x80))), signextend(6, mload(add(p, 0x60)))) }
        }
        if (!ok) return (false, 0);
        int256 w = int256(uint256(a - b));
        int256 q = d / w;
        // [MU-101] round toward negative infinity
        if (d < 0 && d % w != 0) --q;
        if (q < TickMath.MIN_TICK) q = TickMath.MIN_TICK;
        if (q > TickMath.MAX_TICK) q = TickMath.MAX_TICK;
        tick = int24(q);
    }

    // ------------------------------------------------------------------ helpers

    /// @dev The pool's current sqrt price and tick (a seam for the test harness).
    function _slot0() internal view virtual returns (uint160 sqrtP, int24 tick) {
        (sqrtP, tick,,) = poolManager.getSlot0(POOL_ID);
    }

    /// @dev Modifies the witness position and settles the hook's deltas against its ERC-6909 claims.
    function _modify(PoolKey memory key, ModifyLiquidityParams memory p) internal {
        (BalanceDelta d,) = poolManager.modifyLiquidity(key, p, "");
        _witnessReserve0 = _settleOne(CURRENCY0, d.amount0(), _witnessReserve0);
        _witnessReserve1 = _settleOne(CURRENCY1, d.amount1(), _witnessReserve1);
    }

    /// @dev A positive delta is taken as claims, a negative one is paid by burning claims; returns the new reserve.
    function _settleOne(Currency c, int128 a, uint128 reserve) internal returns (uint128) {
        if (a > 0) {
            poolManager.mint(address(this), c.toId(), uint128(a));
            return reserve + uint128(a);
        }
        if (a < 0) {
            poolManager.burn(address(this), c.toId(), uint128(-a));
            return reserve - uint128(-a);
        }
        return reserve;
    }

    function _witSalt(uint32 epoch) internal pure returns (bytes32) {
        return keccak256(abi.encode("witness", epoch));
    }

    function _satAdd(uint64 x, uint256 y) internal pure returns (uint64) {
        uint256 z = uint256(x) + y;
        return z > type(uint64).max ? type(uint64).max : uint64(z);
    }
}
