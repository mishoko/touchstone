// SPDX-License-Identifier: MIT
pragma solidity 0.8.26;

import {IPoolManager} from "@uniswap/v4-core/src/interfaces/IPoolManager.sol";
import {IHooks} from "@uniswap/v4-core/src/interfaces/IHooks.sol";
import {PoolKey} from "@uniswap/v4-core/src/types/PoolKey.sol";
import {PoolId} from "@uniswap/v4-core/src/types/PoolId.sol";
import {Currency} from "@uniswap/v4-core/src/types/Currency.sol";
import {BalanceDelta} from "@uniswap/v4-core/src/types/BalanceDelta.sol";
import {ModifyLiquidityParams} from "@uniswap/v4-core/src/types/PoolOperation.sol";
import {FullMath} from "@uniswap/v4-core/src/libraries/FullMath.sol";
import {SafeCast} from "@openzeppelin/contracts/utils/math/SafeCast.sol";
import {ERC20} from "@openzeppelin/contracts/token/ERC20/ERC20.sol";
import {IERC20} from "@openzeppelin/contracts/token/ERC20/IERC20.sol";
import {SafeERC20} from "@openzeppelin/contracts/token/ERC20/utils/SafeERC20.sol";
import {ITouchstoneFactory, Pending} from "./interfaces/ITouchstoneFactory.sol";
import {ITouchstoneHook} from "./interfaces/ITouchstoneHook.sol";
import {Tier} from "./types/Tier.sol";
import {Holdings} from "./types/Holdings.sol";
import {Markout} from "./types/Markout.sol";
import {Bank, Op, V} from "./types/Bank.sol";
import {FeeBank} from "./libraries/FeeBank.sol";
import {EpochRecord, F_WITNESS_MISSING} from "./types/EpochRecord.sol";

/// @title VaultStorage
/// @notice The one storage layout of a vault and its extension (spec §4.12): the ERC-20 state, then the vault's
///         fields in this order. Both `TouchstoneVault` and `TouchstoneVaultExtension` inherit it and declare no
///         storage of their own; CI compares their layout snapshots.
abstract contract VaultStorage is ERC20 {
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
    /// @dev The block of the curator's last action (0 = none yet: no cooldown before the first action).
    uint40 internal _lastAction;
    uint128 internal _baseOwed;
    bool internal _seeded;
    address public curator;
    Bank internal _bank;
    /// @dev The pending action's record (meaningful while `_tAction != 0`): the holdings before and after the action,
    ///      its immediate charge and its reserve (values in token1 units), and the spots, burn and supply the fee-aware
    ///      settlement needs (`Markout`; its first slot is the charge and the reserve, as before).
    Holdings internal _pBefore;
    Holdings internal _pAfter;
    Markout internal _pend;
    /// @dev The epoch records (spec §3.9), keyed by epoch; the open one is `_records[_epoch]`.
    mapping(uint32 => EpochRecord) internal _records;
}

/// @title VaultConfig
/// @notice Every immutable of a vault and its extension, set in one constructor from the factory's `pending()`, so the
///         two contracts of one vault carry the same configuration (checked by the vault through `CONFIG_HASH`).
abstract contract VaultConfig is VaultStorage {
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
    int24 internal immutable REF_BAND;
    int24 internal immutable BAND_SWAP;
    uint256 internal immutable COOLDOWN;
    uint256 internal immutable RESERVE_RATIO;
    uint256 internal immutable MAX_SWAP_ACTION;
    uint256 internal immutable MAX_SWAP_EPOCH;
    uint256 internal immutable MAX_SWAPS;
    int24 internal immutable W_TICKS;
    uint256 internal immutable DELTA_MIN;
    uint256 internal immutable M_PER_BLOCK;
    PoolId internal immutable POOL_ID;
    bytes32 public immutable TIER_HASH;
    /// @dev `keccak256(abi.encode(manager, factory, hook, key, tier))`: the vault requires its extension's to equal its own.
    bytes32 internal immutable CONFIG;

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
        CONFIG = keccak256(abi.encode(MANAGER, msg.sender, p.hook, p.key, t));
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
        REF_BAND = t.REF_BAND;
        BAND_SWAP = t.BAND_SWAP;
        COOLDOWN = t.COOLDOWN;
        RESERVE_RATIO = t.RESERVE_RATIO;
        MAX_SWAP_ACTION = t.MAX_SWAP_ACTION;
        MAX_SWAP_EPOCH = t.MAX_SWAP_EPOCH;
        MAX_SWAPS = t.MAX_SWAPS;
        W_TICKS = t.W_TICKS;
        DELTA_MIN = t.DELTA_MIN;
        M_PER_BLOCK = t.m_perBlock;
        POOL_ID = PoolId.wrap(keccak256(abi.encode(p.key)));
    }
}

/// @title VaultCommon
/// @notice The events, errors and helpers shared by a vault and its extension (one source; no helper is copied).
///         Every event is emitted from the vault's address: the extension's code runs only by `delegatecall`.
abstract contract VaultCommon is VaultConfig {
    using SafeERC20 for IERC20;

    // ------------------------------------------------------------------ events and errors
    event Deposit(address indexed owner, uint256 assets0, uint256 assets1, uint256 shares);
    event Withdraw(address indexed owner, uint256 shares, uint256 out0, uint256 out1);
    event Checkpoint(uint64 tauV, uint64 tauB, uint256 nav0, uint64 delta, uint256 base, uint128 L, int24 lo, int24 hi);
    event BreachProcessed(uint32 count, uint40 lastBlock, uint256 forfeited);
    /// @notice A checkpoint closed a base amount into `baseOwed` because no price was available.
    event CheckpointDeferred();
    event Settled(uint256 charge0, uint256 extra, int24 tickMk, bool fallbackUsed);
    event Action(uint8 kind, uint256 charge, uint160 pStar, uint256 burnedBank, uint256 burnedBond);
    event Claim(address indexed to, uint256 shares);
    event BondTopUp(uint256 shares);
    event BondReleased(address indexed to, uint256 shares);
    event Retired(uint256 forfeited);
    /// @notice `Bank.mode` changed (place, breach, removal, retire).
    event ModeChanged(uint8 from, uint8 to);
    /// @notice A closed epoch record (at most two per checkpoint: the closed epoch and one gap record).
    event EpochRecorded(uint32 indexed e, EpochRecord r);

    error OnlyFactory();
    error OnlyPoolManager();
    error NotSeeded();
    error ZeroShares();
    error MarkoutPending();
    error Slippage();
    error NavCap();
    error BondMin();
    error RefGas();
    error OnlyCurator();
    error Fence();
    error Cooldown();
    error MaxSwaps();
    error SwapCap();
    error MaxCharge();
    error Width();
    error Alignment();
    error NotContained();
    error DeltaMin();
    error Move();
    /// @notice The vault's fallback refuses a selector outside the extension's allow-list (and empty calldata).
    error UnknownSelector();
    /// @notice The extension refuses a call that is not a `delegatecall` from its vault.
    error NotDelegated();
    /// @notice The vault's constructor found an extension that is not this vault's (no code, another vault, or
    ///         another configuration).
    error BadExtension();

    // ------------------------------------------------------------------ helpers

    /// @dev Only the curator.
    function _onlyCurator() internal view {
        if (msg.sender != curator) revert OnlyCurator();
    }

    /// @dev Books a PoolManager delta of the vault (a modify or a swap; negative = owed by the vault) into idle through
    ///      claims: a positive amount is minted as claims and credited, a negative one is paid by burning claims and
    ///      debited.
    function _book(BalanceDelta d) internal {
        _idle0 = _bookOne(C0, _idle0, d.amount0());
        _idle1 = _bookOne(C1, _idle1, d.amount1());
    }

    function _bookOne(Currency c, uint128 idle, int128 a) internal returns (uint128) {
        if (a > 0) {
            MANAGER.mint(address(this), c.toId(), uint128(a));
            return SafeCast.toUint128(uint256(idle) + uint128(a));
        }
        if (a < 0) MANAGER.burn(address(this), c.toId(), uint128(-a));
        return idle - uint128(-a);
    }

    /// @dev Applies a bank op whose first return is a share burn, burns it from the vault's held shares, and returns
    ///      the op's third return (the forfeited or burned amount).
    function _burnBank(Op op, bytes memory args) internal returns (uint256 a) {
        uint256 burned;
        (burned,, a,) = FeeBank.bankApply(_bank, op, args);
        // [MU-167] [MU-173] [MU-174] the bank's burn leaves the supply, exactly
        if (burned > 0) _burn(address(this), burned);
    }

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

    /// @dev Emits `ModeChanged` if `Bank.mode` differs from `m0`.
    function _modeChanged(uint8 m0) internal {
        uint8 m = _bank.mode;
        // [MU-288] only on a change
        if (m != m0) emit ModeChanged(m0, m);
    }

    /// @dev Opens the record of epoch `e` at this block from the hook's observation (`witnessMissing` until a roll).
    function _openRecord(uint32 e, int24 med9, uint64 tauV, uint64 tauB) internal {
        EpochRecord storage r = _records[e];
        (r.start, r.med9Start, r.tauV, r.tauB) = (uint40(block.number), med9, tauV, tauB);
        // [MU-289] a record opens with its witness missing (S19D-02)
        r.flags |= F_WITNESS_MISSING;
    }

    /// @dev Saturating adds into the record's fields (an accumulation never reverts).
    function _a96(uint96 x, uint256 y) internal pure returns (uint96) {
        uint256 z = uint256(x) + y;
        return z > type(uint96).max ? type(uint96).max : uint96(z);
    }

    function _a192(uint192 x, uint256 y) internal pure returns (uint192) {
        uint256 z = uint256(x) + y;
        return z > type(uint192).max ? type(uint192).max : uint192(z);
    }

    function _a128(uint128 x, uint256 y) internal pure returns (uint128) {
        uint256 z = uint256(x) + y;
        return z > type(uint128).max ? type(uint128).max : uint128(z);
    }

    function _min(uint256 a, uint256 b) internal pure returns (uint256) {
        return a < b ? a : b;
    }

    function _u128(uint256 x) internal pure returns (uint128) {
        return x > type(uint128).max ? type(uint128).max : uint128(x);
    }
}
