// SPDX-License-Identifier: MIT
pragma solidity 0.8.26;

import {BalanceDelta} from "@uniswap/v4-core/src/types/BalanceDelta.sol";
import {TickMath} from "@uniswap/v4-core/src/libraries/TickMath.sol";
import {FullMath} from "@uniswap/v4-core/src/libraries/FullMath.sol";
import {SafeCast} from "@openzeppelin/contracts/utils/math/SafeCast.sol";
import {ITouchstoneFactory} from "./interfaces/ITouchstoneFactory.sol";
import {Obs} from "./types/Obs.sol";
import {Entry} from "./types/Entry.sol";
import {Ladder} from "./libraries/Ladder.sol";
import {ExecLedger} from "./libraries/ExecLedger.sol";
import {FeeBank} from "./libraries/FeeBank.sol";
import {Op} from "./types/Bank.sol";
import {EpochRecord, F_STALE, F_WITNESS_MISSING, F_WITNESS_FALLBACK} from "./types/EpochRecord.sol";
import {VaultCommon} from "./TouchstoneVaultBase.sol";

/// @title TouchstoneVaultExtension
/// @notice The cold paths of one vault (spec §4.12, lever (c)): `seed`, `deposit`, `withdraw` and the curator's bank
///         entries `claim`, `topUpBond`, `retire`, `releaseBond`, with the open `poke` and `processBreach`. Its code runs only by
///         `delegatecall` from its vault, on the vault's storage and under the vault's address: the vault's fallback
///         forwards the allow-listed selectors (holding the vault's reentrancy lock), each moved entry point calls
///         `MANAGER.unlock`, and the vault's `unlockCallback` runs the prologue and the mode gate and then delegates
///         the flow's body to `dispatch`. The factory binds the pair: both addresses are computed before either is
///         created, from code hashes fixed in the factory.
/// @dev Every non-view external function refuses a direct call (`address(this) == SELF`). Called directly, the
///      inherited ERC-20 functions act on this contract's own empty storage only; through the vault they are never
///      reached (the vault's own selectors win).
contract TouchstoneVaultExtension is VaultCommon {
    /// @notice The one vault this extension serves.
    address public immutable VAULT;
    /// @notice This contract's own address: a call whose `address(this)` is this is a direct call, not a delegatecall.
    address public immutable SELF;

    /// @notice The gas `poke` must hold before the witness roll (F-048 (b), spec §4.6 poke): two 63/64 hops above the
    ///         worst measured `witnessStep` (a removal and an add on fresh ticks), so a caller cannot starve the roll into
    ///         a recorded failure. Measured in G-3's pokes (s19): `rollWitness` 164,796 / 180,406 gas (`witnessStep`
    ///         159,249 / 174,859), so two hops and 30k of overhead need ≈ 220k; 400k covers cold accounts and fresh ticks.
    uint256 public constant WITNESS_GAS_MIN = 400_000;

    constructor() {
        VAULT = ITouchstoneFactory(msg.sender).pending().vault;
        SELF = address(this);
    }

    /// @notice The configuration commitment the vault's constructor compares with its own.
    function CONFIG_HASH() external view returns (bytes32) {
        return CONFIG;
    }

    modifier delegated() {
        // [MU-263] the extension refuses a direct call: its code runs only on its vault's storage
        if (address(this) == SELF) revert NotDelegated();
        _;
    }

    // ------------------------------------------------------------------ entry points (reached through the vault)

    /// @notice Called once by the factory, which has transferred `a0 + wit0` and `a1 + wit1` to the vault.
    function seed(uint256 a0, uint256 a1, address curator_, uint256 wit0, uint256 wit1) external delegated {
        if (msg.sender != FACTORY) revert OnlyFactory();
        if (_seeded) revert OnlyFactory();
        if (a0 + a1 == 0) revert ZeroShares();
        curator = curator_;
        MANAGER.unlock(abi.encode(Entry.Seed, address(0), abi.encode(a0, a1, wit0, wit1)));
    }

    /// @notice Mints `shares` to `to` against a pro-rata slice of every bucket, paid by the caller (rounded up).
    function deposit(uint256 shares, uint256 max0, uint256 max1, address to)
        external
        delegated
        returns (uint256 a0, uint256 a1)
    {
        if (shares == 0) revert ZeroShares();
        return abi.decode(
            MANAGER.unlock(abi.encode(Entry.Deposit, msg.sender, abi.encode(shares, max0, max1, to))), (uint256, uint256)
        );
    }

    /// @notice Burns `shares` of the caller and pays `to` a pro-rata slice of every bucket in kind (rounded down).
    function withdraw(uint256 shares, uint256 min0, uint256 min1, address to)
        external
        delegated
        returns (uint256 a0, uint256 a1)
    {
        if (shares == 0) revert ZeroShares();
        return abi.decode(
            MANAGER.unlock(abi.encode(Entry.Withdraw, msg.sender, abi.encode(shares, min0, min1, to))), (uint256, uint256)
        );
    }

    /// @notice Transfers the curator's vested shares to it (open in every mode).
    function claim() external delegated {
        // [MU-284] curator only
        _onlyCurator();
        _enter(Entry.Claim, "");
    }

    /// @notice Moves `shares` of the curator's own shares into the bond (NORMAL and BREACHED).
    function topUpBond(uint256 shares) external delegated {
        // [MU-285] curator only
        _onlyCurator();
        _enter(Entry.TopUpBond, abi.encode(shares));
    }

    /// @notice The curator exits: all unvested pay is forfeited to the drip and the vault moves to EXITED.
    function retire() external delegated {
        // [MU-280] curator only
        _onlyCurator();
        _enter(Entry.Retire, "");
    }

    /// @notice Returns the whole bond to the curator, in EXITED, at or after `exitBlock + V·EPOCH`.
    function releaseBond() external delegated {
        // [MU-286] curator only
        _onlyCurator();
        _enter(Entry.ReleaseBond, "");
    }

    /// @notice Open: the prologue (settlement, snapshot, collect, checkpoint), then the witness roll.
    function poke() external delegated {
        _enter(Entry.Poke, "");
    }

    /// @notice Open: the prologue alone (its checkpoint processes pending breaches).
    function processBreach() external delegated {
        _enter(Entry.ProcessBreach, "");
    }

    function _enter(Entry entry, bytes memory args) internal {
        MANAGER.unlock(abi.encode(entry, msg.sender, args));
    }

    /// @notice The moved flows' bodies, run inside the vault's `unlockCallback` (by `delegatecall`): `Seed` at once
    ///         (no prologue), every other entry after the vault's step 0, prologue, checkpoint and mode gate.
    function dispatch(Entry entry, address payer, bytes calldata args) external delegated returns (bytes memory) {
        // defence in depth: only inside the vault's unlockCallback (unreachable on its own behind the vault's
        // allow-list and the SELF check, so it carries no mutant)
        if (msg.sender != address(MANAGER)) revert OnlyPoolManager();
        if (entry == Entry.Seed) {
            // seeding happens once (defence in depth: the external `seed` has checked it, and no other path encodes
            // `Entry.Seed`, so this carries no mutant)
            if (_seeded) revert OnlyFactory();
            return _seed(args);
        }
        if (entry == Entry.Deposit) return _deposit(payer, args);
        if (entry == Entry.Withdraw) return _withdraw(payer, args);
        if (entry == Entry.Claim) return _pay(Op.Claim, "");
        if (entry == Entry.ReleaseBond) {
            // the whole bond (FeeBank: BadMode outside EXITED, BondLocked before exitBlock + releaseDelay)
            return _pay(Op.Release, abi.encode(type(uint256).max, uint40(block.number)));
        }
        if (entry == Entry.TopUpBond) {
            uint256 s = abi.decode(args, (uint256));
            // [MU-281] [MU-311] the curator's own shares move into the vault's custody with the bond
            _transfer(payer, address(this), s);
            FeeBank.bankApply(_bank, Op.TopUp, args);
            emit BondTopUp(s);
            return "";
        }
        if (entry == Entry.Retire) {
            // [MU-279] the deferred base is forfeited with the rest (S16B-05): the checkpoint mints `baseOwed` in any
            // mode, so this write is what keeps it out of EXITED (the prologue's checkpoint has already closed the
            // segment; the forfeited value enters the epoch record with the records, s19)
            // [MU-298] the record: the forfeited deferred base's value, read before it is zeroed (S18D-05)
            EpochRecord storage r = _records[_epoch];
            r.forfeitValue = _a96(r.forfeitValue, _baseOwed);
            _baseOwed = 0;
            uint8 m0 = _bank.mode;
            uint256 forfeited = _burnBank(Op.Retire, abi.encode(uint40(block.number)));
            r.forfeited = _a128(r.forfeited, forfeited);
            _modeChanged(m0);
            emit Retired(forfeited);
            return "";
        }
        if (entry == Entry.Poke) {
            // [MU-299] the caller cannot starve the roll into a recorded failure (F-048 (b))
            if (gasleft() < _witnessGasMin()) revert RefGas();
            uint32 e = _epoch;
            // [MU-282] the witness roll (fail-soft in the hook; it ignores an epoch already rolled)
            (,, uint128 lw, bool fb, uint8 st) = HOOK.rollWitness(e);
            // the record (spec §3.9, S19D-02): it opened with `witnessMissing`; a successful roll writes the fallback flag
            // and clears `witnessMissing` if the witness is live (L_W > 0). A failed roll (status 2) needs no write: the
            // flag can only be clear after a successful roll of this epoch, after which the hook ignores every roll.
            if (st == 1) {
                EpochRecord storage r = _records[e];
                // [MU-300]
                uint8 f = r.flags & ~(F_WITNESS_MISSING | F_WITNESS_FALLBACK);
                if (fb) f |= F_WITNESS_FALLBACK;
                // [MU-301] an unfunded witness (L_W = 0) is missing
                if (lw == 0) f |= F_WITNESS_MISSING;
                r.flags = f;
            }
            return "";
        }
        // [MU-172] processBreach is the prologue alone (an entry without its branch would reach the tail below)
        if (entry == Entry.ProcessBreach) return "";
        // reachable only by entries the vault never delegates (SetPositions, RebalanceSwap, Settle): defence in
        // depth, so it carries no mutant
        revert Ladder.BadMode();
    }

    /// @dev A bank op that pays shares (`Claim`, `Release`): the vault's held shares to the curator.
    function _pay(Op op, bytes memory a) internal returns (bytes memory) {
        (, uint256 pay,,) = FeeBank.bankApply(_bank, op, a);
        // [MU-283] the payment leaves the vault's custody to the curator
        _transfer(address(this), curator, pay);
        if (op == Op.Claim) emit Claim(curator, pay);
        else emit BondReleased(curator, pay);
        return "";
    }

    /// @notice The epoch roll (spec §4.6 step (6), §3.9): closes the record of the stored epoch, writes one gap record
    ///         if epochs were skipped (both flagged `stale`), emits `EpochRecorded` for each (at most two), opens the
    ///         record of `ne` and advances the epoch. Run only inside the vault's checkpoint (by `delegatecall`).
    ///         It refuses a direct call (`delegated`); it is off the fallback's allow-list, so only the vault's own code
    ///         reaches it (no `MANAGER` check: the test harness scripts the checkpoint outside `unlock`, X14-05).
    function roll(Obs calldata o, uint32 ne) external delegated {
        uint32 old = _epoch;
        EpochRecord storage r = _records[old];
        // [MU-302] the end fields; tauV/tauB become the differences over the window
        (r.end, r.med9End) = (uint40(block.number), o.med9);
        (r.tauV, r.tauB) = (o.tauV > r.tauV ? o.tauV - r.tauV : 0, o.tauB > r.tauB ? o.tauB - r.tauB : 0);
        // [MU-303] skipped epochs: `old` spans them and one gap record marks them
        if (ne > old + 1) {
            r.flags |= F_STALE;
            EpochRecord storage g = _records[old + 1];
            (g.start, g.end, g.med9Start, g.med9End) = (uint40(block.number), uint40(block.number), o.med9, o.med9);
            g.flags |= F_STALE | F_WITNESS_MISSING;
            emit EpochRecorded(old, r);
            emit EpochRecorded(old + 1, g);
        } else {
            emit EpochRecorded(old, r);
        }
        // [MU-304] the new record opens and the epoch advances
        _openRecord(ne, o.med9, o.tauV, o.tauB);
        // [MU-170]
        _epoch = ne;
    }

    /// @dev The witness roll's gas floor (a seam for the test harness's control).
    function _witnessGasMin() internal view virtual returns (uint256) {
        return WITNESS_GAS_MIN;
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
        // [MU-305] the first record opens at seed
        _openRecord(o.epoch, o.med9, o.tauV, o.tauB);
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
}
