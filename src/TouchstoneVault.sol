// SPDX-License-Identifier: MIT
pragma solidity 0.8.26;

import {IPoolManager} from "@uniswap/v4-core/src/interfaces/IPoolManager.sol";
import {Extsload} from "@uniswap/v4-core/src/Extsload.sol";
import {IUnlockCallback} from "@uniswap/v4-core/src/interfaces/callback/IUnlockCallback.sol";
import {IHooks} from "@uniswap/v4-core/src/interfaces/IHooks.sol";
import {PoolKey} from "@uniswap/v4-core/src/types/PoolKey.sol";
import {Currency} from "@uniswap/v4-core/src/types/Currency.sol";
import {BalanceDelta} from "@uniswap/v4-core/src/types/BalanceDelta.sol";
import {ModifyLiquidityParams} from "@uniswap/v4-core/src/types/PoolOperation.sol";
import {TickMath} from "@uniswap/v4-core/src/libraries/TickMath.sol";
import {FullMath} from "@uniswap/v4-core/src/libraries/FullMath.sol";
import {SafeCast} from "@openzeppelin/contracts/utils/math/SafeCast.sol";
import {ERC20} from "@openzeppelin/contracts/token/ERC20/ERC20.sol";
import {IERC20} from "@openzeppelin/contracts/token/ERC20/IERC20.sol";
import {SafeERC20} from "@openzeppelin/contracts/token/ERC20/utils/SafeERC20.sol";
import {ReentrancyGuardTransient} from "@openzeppelin/contracts/utils/ReentrancyGuardTransient.sol";
import {ITouchstoneFactory, Pending} from "./interfaces/ITouchstoneFactory.sol";
import {ITouchstoneHook} from "./interfaces/ITouchstoneHook.sol";
import {Tier} from "./types/Tier.sol";
import {Obs} from "./types/Obs.sol";
import {Entry} from "./types/Entry.sol";
import {Holdings} from "./types/Holdings.sol";
import {Bank, Op, RING, V} from "./types/Bank.sol";
import {Ladder} from "./libraries/Ladder.sol";
import {ExecLedger} from "./libraries/ExecLedger.sol";
import {FeeBank} from "./libraries/FeeBank.sol";

/// @title TouchstoneVault
/// @notice A single-position Uniswap v4 vault whose curator is paid from a bank of deferred shares. Shares are pro
///         rata in kind over three buckets kept in storage (idle token0, idle token1, liquidity); idle balances are
///         PoolManager ERC-6909 claims tracked internally, so unsolicited transfers of tokens, claims or shares are
///         never read. Every entry point runs inside the vault's own `unlock`: the prologue snapshots the hook's
///         clock, collects the position's fees into idle, then checkpoints; the mode gate runs after the prologue.
/// @dev This build carries seed, deposit and withdraw; the checkpoint body, the curator actions and the bank entry
///      points are added in later builds. The vault has no state views: every field is read through `extsload` at
///      the slots of its storage-layout snapshot (the Lens decodes them), and the tier through `TIER_HASH`.
contract TouchstoneVault is ERC20, Extsload, IUnlockCallback, ReentrancyGuardTransient {
    using SafeERC20 for IERC20;

    uint256 internal constant WAD = 1e18;
    uint256 public constant S_SEED = 1e24;
    uint256 public constant DEAD = 1e15;
    address internal constant DEAD_ADDRESS = address(0xdEaD);

    IPoolManager public immutable MANAGER;
    address public immutable FACTORY;
    ITouchstoneHook public immutable HOOK;
    Currency internal immutable C0;
    Currency internal immutable C1;
    uint24 internal immutable FEE;
    int24 internal immutable SPACING;
    uint64 internal immutable BOND_RATIO;
    uint128 internal immutable BOND_MIN;
    uint128 internal immutable NAV_MAX;
    uint160 internal immutable REF_SQRT_PRICE;
    uint40 internal immutable DRIP_BLOCKS;
    uint40 internal immutable RELEASE_DELAY;
    uint256 internal immutable EPOCH;
    uint256 internal immutable BLOCKTIME;
    uint256 internal immutable H_MARKOUT;
    uint256 internal immutable REF_GAS_CAP;
    int24 internal immutable FEE_TICKS;
    bytes32 public immutable TIER_HASH;

    // ------------------------------------------------------------------ storage (spec field widths)
    uint128 internal _idle0;
    uint128 internal _idle1;
    uint128 internal _L;
    int24 internal _lo;
    int24 internal _hi;
    uint128 internal _nav0;
    uint64 internal _delta0;
    uint64 internal _tauV0;
    uint40 internal _segStart;
    uint32 internal _epoch;
    /// @dev The epoch's swap counters (the curator's swap caps); reset at every epoch roll.
    uint64 internal _epochSwapFrac;
    uint16 internal _swapCount;
    /// @dev The pending curator action's block timestamp (0 = none); the rest of its record follows `_bank`.
    uint40 internal _tAction;
    uint128 internal _baseOwed;
    bool internal _seeded;
    address public curator;
    Bank internal _bank;
    /// @dev The pending action's record (meaningful while `_tAction != 0`): the holdings before and after the action,
    ///      its immediate charge and its reserve (values in token1 units).
    Holdings internal _pBefore;
    Holdings internal _pAfter;
    uint128 internal _pCharge0;
    uint128 internal _pReserve;

    // ------------------------------------------------------------------ events and errors
    event Deposit(address indexed owner, uint256 assets0, uint256 assets1, uint256 shares);
    event Withdraw(address indexed owner, uint256 shares, uint256 out0, uint256 out1);
    event Checkpoint(uint64 tauV, uint64 tauB, uint256 nav0, uint64 delta, uint256 base, uint128 L, int24 lo, int24 hi);
    event BreachProcessed(uint32 count, uint40 lastBlock, uint256 forfeited);
    event Settled(uint256 charge0, uint256 extra, int24 tickMk, bool fallbackUsed);

    error OnlyFactory();
    error OnlyPoolManager();
    error NotSeeded();
    error ZeroShares();
    error MarkoutPending();
    error Slippage();
    error NavCap();
    error BondMin();
    error RefGas();

    constructor() ERC20("Touchstone Vault Share", "TSV") {
        FACTORY = msg.sender;
        MANAGER = IPoolManager(ITouchstoneFactory(msg.sender).POOL_MANAGER());
        Pending memory p = ITouchstoneFactory(msg.sender).pending();
        HOOK = ITouchstoneHook(p.hook);
        C0 = p.key.currency0;
        C1 = p.key.currency1;
        FEE = p.key.fee;
        SPACING = p.key.tickSpacing;
        Tier memory t = p.tier;
        // the factory checks this commitment; off-chain readers (the Lens) verify a tier against it
        TIER_HASH = keccak256(abi.encode(t));
        BOND_RATIO = t.BOND_RATIO;
        BOND_MIN = t.BOND_MIN;
        NAV_MAX = t.NAV_MAX;
        REF_SQRT_PRICE = t.REF_SQRT_PRICE;
        DRIP_BLOCKS = t.FORFEIT_DRIP_BLOCKS;
        RELEASE_DELAY = uint40(uint256(V) * t.EPOCH);
        EPOCH = t.EPOCH;
        BLOCKTIME = t.GRID_SEC / t.GRID;
        H_MARKOUT = t.H_MARKOUT;
        REF_GAS_CAP = t.REF_GAS_CAP;
        FEE_TICKS = t.FEE_TICKS;
    }

    // ------------------------------------------------------------------ entry points

    /// @notice Called once by the factory, which has transferred `a0 + wit0` and `a1 + wit1` to this vault.
    function seed(uint256 a0, uint256 a1, address curator_, uint256 wit0, uint256 wit1) external nonReentrant {
        if (msg.sender != FACTORY) revert OnlyFactory();
        if (_seeded) revert OnlyFactory();
        if (a0 + a1 == 0) revert ZeroShares();
        curator = curator_;
        MANAGER.unlock(abi.encode(Entry.Seed, address(0), abi.encode(a0, a1, wit0, wit1)));
    }

    /// @notice Mints `shares` to `to` against a pro-rata slice of every bucket, paid by the caller (rounded up).
    function deposit(uint256 shares, uint256 max0, uint256 max1, address to)
        external
        nonReentrant
        returns (uint256 a0, uint256 a1)
    {
        if (shares == 0) revert ZeroShares();
        return abi.decode(
            MANAGER.unlock(abi.encode(Entry.Deposit, msg.sender, abi.encode(shares, max0, max1, to))), (uint256, uint256)
        );
    }

    /// @notice Burns `shares` of the caller and pays `to` a pro-rata slice of every bucket in kind (rounded down).
    // [MU-160] every entry point holds the reentrancy lock across its whole unlock
    function withdraw(uint256 shares, uint256 min0, uint256 min1, address to)
        external
        nonReentrant
        returns (uint256 a0, uint256 a1)
    {
        if (shares == 0) revert ZeroShares();
        return abi.decode(
            MANAGER.unlock(abi.encode(Entry.Withdraw, msg.sender, abi.encode(shares, min0, min1, to))), (uint256, uint256)
        );
    }

    /// @notice Settles a pending curator action whose markout window has closed (a no-op otherwise), then checkpoints.
    function settle() external nonReentrant {
        MANAGER.unlock(abi.encode(Entry.Settle, msg.sender, ""));
    }

    function unlockCallback(bytes calldata data) external returns (bytes memory) {
        // [MU-158] only the PoolManager calls back, and it calls back only the unlocker: this makes `payer` trustworthy
        if (msg.sender != address(MANAGER)) revert OnlyPoolManager();
        (Entry entry, address payer, bytes memory args) = abi.decode(data, (Entry, address, bytes));
        if (entry == Entry.Seed) return _seed(args);
        if (!_seeded) revert NotSeeded();
        // [MU-185] step 0, before the snapshot and collect-first: the supply and holdings the settlement prices
        _settle();
        Obs memory o = _prologue();
        // [MU-037] every entry point checkpoints
        _checkpoint(o);
        // [MU-073] the mode gate runs after the prologue checkpoint, which may have moved the mode to EXITED
        if (!Ladder.allowed(_bank.mode, uint8(entry))) revert Ladder.BadMode();
        return _dispatch(entry, payer, args);
    }

    /// @dev The entry points of this build. A test harness extends it; every other entry is unreachable here.
    function _dispatch(Entry entry, address payer, bytes memory args) internal virtual returns (bytes memory) {
        if (entry == Entry.Deposit) return _deposit(payer, args);
        // [MU-172] an entry this build does not implement never falls through to a flow
        if (entry == Entry.Withdraw) return _withdraw(payer, args);
        // settle() is the prologue alone
        if (entry == Entry.Settle) return "";
        revert Ladder.BadMode();
    }

    // ------------------------------------------------------------------ flows

    function _seed(bytes memory args) internal returns (bytes memory) {
        (uint256 a0, uint256 a1, uint256 wit0, uint256 wit1) = abi.decode(args, (uint256, uint256, uint256, uint256));
        HOOK.snapshotIfNewBlock();
        _payIn(C0, address(this), a0 + wit0);
        _payIn(C1, address(this), a1 + wit1);
        _mintClaims(C0, a0);
        _mintClaims(C1, a1);
        // the witness funding goes to the hook as claims; it is never part of the vault's buckets
        if (wit0 > 0) MANAGER.mint(address(HOOK), C0.toId(), wit0);
        if (wit1 > 0) MANAGER.mint(address(HOOK), C1.toId(), wit1);
        HOOK.fundWitness(wit0, wit1);
        // [MU-066] the buckets hold the seed amounts only
        _idle0 = SafeCast.toUint128(a0);
        _idle1 = SafeCast.toUint128(a1);
        _mint(DEAD_ADDRESS, DEAD);
        _mint(address(this), S_SEED - DEAD);
        _bank.bondShares = uint128(S_SEED - DEAD);
        HOOK.armAtSeed();
        // [MU-130] [MU-118] the bond's value at the tier's immutable reference price, not the seed price
        (uint256 navRef,) = ExecLedger.value(_holdings(), REF_SQRT_PRICE);
        // [MU-161] the bond may equal BOND_MIN
        if (FullMath.mulDiv(navRef, S_SEED - DEAD, S_SEED) < BOND_MIN) revert BondMin();
        Obs memory o = HOOK.obs();
        (uint256 navAtRef,) = ExecLedger.value(_holdings(), TickMath.getSqrtPriceAtTick(o.refTick));
        _nav0 = _u128(navAtRef);
        _delta0 = 0;
        _epoch = o.epoch;
        _segStart = uint40(block.number);
        _tauV0 = o.tauV;
        // [MU-074] the drip length and the release delay are written once, at seed
        _bank.dripBlocks = DRIP_BLOCKS;
        _bank.releaseDelay = RELEASE_DELAY;
        _seeded = true;
        return "";
    }

    function _deposit(address payer, bytes memory args) internal returns (bytes memory) {
        (uint256 s, uint256 max0, uint256 max1, address to) = abi.decode(args, (uint256, uint256, uint256, address));
        // [MU-152] no deposit while a curator action awaits its markout settlement
        if (_tAction != 0) revert MarkoutPending();
        uint256 S = totalSupply();
        // [MU-159] buckets come from storage, never from balances
        // [MU-016] [MU-134] every bucket is charged rounded up (>= 1 wei when non-zero)
        uint256 d0 = FullMath.mulDivRoundingUp(_idle0, s, S);
        uint256 d1 = FullMath.mulDivRoundingUp(_idle1, s, S);
        // [MU-162] the L leg too
        uint256 dL = FullMath.mulDivRoundingUp(_L, s, S);
        uint256 p0;
        uint256 p1;
        if (dL > 0) {
            BalanceDelta d = _modify(int256(dL));
            (p0, p1) = (uint256(int256(-d.amount0())), uint256(int256(-d.amount1())));
            _L = SafeCast.toUint128(_L + dL);
        }
        uint256 a0 = p0 + d0;
        uint256 a1 = p1 + d1;
        if (a0 > max0 || a1 > max1) revert Slippage();
        _payIn(C0, payer, a0);
        _payIn(C1, payer, a1);
        _mintClaims(C0, d0);
        _mintClaims(C1, d1);
        // custody buckets never saturate: a deposit that would overflow reverts
        _idle0 = SafeCast.toUint128(_idle0 + d0);
        _idle1 = SafeCast.toUint128(_idle1 + d1);
        _mint(to, s);
        uint256 S2 = S + s;
        // [MU-019] [MU-023] the bond cap in shares: S'·BOND_RATIO <= bondShares·WAD
        if (S2 * BOND_RATIO > uint256(_bank.bondShares) * WAD) revert NavCap();
        // epilogue: the price-free re-base of NAV0
        uint256 navAfter = _rebase(S, S2);
        // [MU-157] the tier cap on the re-based NAV0
        if (navAfter > NAV_MAX) revert NavCap();
        emit Deposit(to, a0, a1, s);
        return abi.encode(a0, a1);
    }

    function _withdraw(address owner, bytes memory args) internal returns (bytes memory) {
        (uint256 s, uint256 min0, uint256 min1, address to) = abi.decode(args, (uint256, uint256, uint256, address));
        // [MU-017] the supply before the burn
        uint256 S = totalSupply();
        uint256 o0 = FullMath.mulDiv(_idle0, s, S);
        uint256 o1 = FullMath.mulDiv(_idle1, s, S);
        // [MU-163] the L leg rounds down like the idle legs
        uint256 dL = FullMath.mulDiv(_L, s, S);
        _burn(owner, s);
        uint256 r0;
        uint256 r1;
        if (dL > 0) {
            BalanceDelta d = _modify(-int256(dL));
            (r0, r1) = (uint256(int256(d.amount0())), uint256(int256(d.amount1())));
            _L -= uint128(dL);
        }
        _idle0 -= uint128(o0);
        _idle1 -= uint128(o1);
        if (o0 > 0) MANAGER.burn(address(this), C0.toId(), o0);
        if (o1 > 0) MANAGER.burn(address(this), C1.toId(), o1);
        uint256 a0 = o0 + r0;
        uint256 a1 = o1 + r1;
        if (a0 > 0) MANAGER.take(C0, to, a0);
        if (a1 > 0) MANAGER.take(C1, to, a1);
        if (a0 < min0 || a1 < min1) revert Slippage();
        _rebase(S, S - s);
        emit Withdraw(owner, s, a0, a1);
        return abi.encode(a0, a1);
    }

    // ------------------------------------------------------------------ prologue

    /// @notice Step 0: the markout settlement of a pending action once its window `(tAction + blocktime, tAction +
    ///         blocktime + H_MARKOUT]` has closed (one more block). The read is guarded by `gasleft() ≥ 2·REF_GAS_CAP`;
    ///         a failed read burns the whole reserve. Its arithmetic never reverts; its only revert is the gas guard.
    function _settle() internal {
        uint256 tA = _tAction;
        // [MU-188] due only once the delayed window has closed (one more block)
        if (tA == 0 || block.timestamp < tA + 2 * BLOCKTIME + H_MARKOUT) return;
        // [MU-186] the caller cannot starve the read into the fallback
        if (gasleft() < _settleGasMin()) revert RefGas();
        // [MU-151] the window opens one block after the action's block
        uint256 a = block.timestamp - tA - BLOCKTIME;
        (bool ok, int24 tickMk) = HOOK.tryWindow(uint32(a), uint32(a - H_MARKOUT));
        Bank storage b = _bank;
        uint256 extra = _pReserve;
        // [MU-150] a failed read burns the whole reserve
        uint256 burnS = b.reserveShares;
        if (ok) {
            (extra,, burnS) = ExecLedger.settleExtra(
                _pBefore, _pAfter, tickMk, FEE_TICKS, _pCharge0, extra, _holdings(), totalSupply(), burnS
            );
        }
        // [MU-191] the record clears (a cleared record never settles twice)
        _tAction = 0;
        // [MU-187] the released non-bond reserve enters the current epoch's cohort
        // [MU-190] the bank's clock is the block number (the drip it may restart runs in blocks)
        _burnBank(Op.Settle, abi.encode(burnS, uint32(block.number / EPOCH), uint40(block.number)));
        emit Settled(_pCharge0, extra, tickMk, !ok);
    }

    /// @dev Snapshot, collect first, checkpoint. The markout settlement (step 0) and the checkpoint body are added with
    ///      the curator actions and the bank; the order below is the binding one.
    function _prologue() internal returns (Obs memory o) {
        // [MU-027] the hook's span is closed in this block before anything reads or registers a range
        o = HOOK.snapshotIfNewBlock();
        // [MU-008] collect first: v4 pays all accrued fees on any modify, so sweep them into idle before pricing
        if (_L > 0) {
            BalanceDelta d = _modify(0);
            uint256 f0 = uint256(int256(d.amount0()));
            uint256 f1 = uint256(int256(d.amount1()));
            _mintClaims(C0, f0);
            _mintClaims(C1, f1);
            _idle0 = SafeCast.toUint128(_idle0 + f0);
            _idle1 = SafeCast.toUint128(_idle1 + f1);
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
        // (1) the vest gate, evaluated once: the cache legs, then the live leg only when something is vestable
        bool vestable = _vestable(b, ne);
        bool gateOpen;
        // [MU-069] [MU-093] [MU-106] [MU-164] [MU-165] a fresh, gap-free cached reference inside the registered range
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
            _baseOwed = 0;
            // [MU-123] every pending breach is counted and acknowledged
            uint256 forfeited = _burnBank(Op.Breach, abi.encode(n, now_));
            // [MU-166] the hook's pending count is reduced by what was processed
            HOOK.ackBreaches(n);
            emit BreachProcessed(n, o.lastPendingBlock, forfeited);
            // [MU-050] the breach cutoff, then the segment below closes at now
            _tauV0 = o.tauVAtLastPending;
            _segStart = o.lastPendingBlock;
        }
        // (4) the segment [segStart, now): no base accrues before the curator can place
        _tauV0 = o.tauV;
        _segStart = now_;
        emit Checkpoint(o.tauV, o.tauB, _nav0, _delta0, 0, _L, lo, hi);
        // (5) [MU-030] vest after breach processing: vested with the gate open, else held in maturedPending
        // [MU-171] the gate decides
        if (vestable) FeeBank.bankApply(b, Op.VestTime, abi.encode(ne, gateOpen));
        // (6) the epoch roll
        if (ne > _epoch) {
            // [MU-170]
            _epoch = ne;
            // [MU-132] the swap counters restart every epoch
            _epochSwapFrac = 0;
            _swapCount = 0;
        }
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

    /// @dev Applies a bank op whose first return is a share burn, burns it from the vault's held shares, and returns
    ///      the op's third return (the forfeited or burned amount).
    function _burnBank(Op op, bytes memory args) internal returns (uint256 a) {
        uint256 burned;
        (burned,, a,) = FeeBank.bankApply(_bank, op, args);
        // [MU-167] [MU-173] [MU-174] the bank's burn leaves the supply, exactly
        if (burned > 0) _burn(address(this), burned);
    }

    /// @dev The settlement's gas guard, `2·REF_GAS_CAP` (a seam for the test harness's control).
    function _settleGasMin() internal view virtual returns (uint256) {
        return 2 * REF_GAS_CAP;
    }

    /// @dev The live reference read of the vest gate (a seam for the scripted test harness).
    function _tryRef() internal virtual returns (bool ok, int24 tick) {
        // [MU-076] the gate reads the live reference, never the cache
        return HOOK.tryRef();
    }

    // ------------------------------------------------------------------ internals

    /// @dev The price-free epilogue of a pro-rata flow: NAV0 := NAV0·S'/S; δ0 unchanged.
    function _rebase(uint256 S, uint256 S2) internal returns (uint256 rebased) {
        // [MU-055] [MU-096] no price is read on a deposit or withdrawal
        rebased = FullMath.mulDiv(_nav0, S2, S);
        _nav0 = _u128(rebased);
    }

    function _modify(int256 liquidityDelta) internal returns (BalanceDelta d) {
        (d,) = MANAGER.modifyLiquidity(_key(), ModifyLiquidityParams(_lo, _hi, liquidityDelta, bytes32(0)), "");
    }

    /// @dev Pays `amount` of `c` into the PoolManager from `from` (this vault when `from == address(this)`).
    function _payIn(Currency c, address from, uint256 amount) internal {
        if (amount == 0) return;
        MANAGER.sync(c);
        if (from == address(this)) IERC20(Currency.unwrap(c)).safeTransfer(address(MANAGER), amount);
        else IERC20(Currency.unwrap(c)).safeTransferFrom(from, address(MANAGER), amount);
        MANAGER.settle();
    }

    function _mintClaims(Currency c, uint256 amount) internal {
        if (amount > 0) MANAGER.mint(address(this), c.toId(), amount);
    }

    function _key() internal view returns (PoolKey memory) {
        return PoolKey(C0, C1, FEE, SPACING, IHooks(address(HOOK)));
    }

    function _holdings() internal view returns (Holdings memory) {
        return Holdings(_idle0, _idle1, _L, _lo, _hi);
    }

    function _u128(uint256 x) internal pure returns (uint128) {
        return x > type(uint128).max ? type(uint128).max : uint128(x);
    }

}
