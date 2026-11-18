// SPDX-License-Identifier: MIT
pragma solidity 0.8.26;

import {EpochRecord} from "../../src/types/EpochRecord.sol";

import {BalanceDelta} from "@uniswap/v4-core/src/types/BalanceDelta.sol";
import {PoolId} from "@uniswap/v4-core/src/types/PoolId.sol";
import {StateLibrary} from "@uniswap/v4-core/src/libraries/StateLibrary.sol";
import {TickMath} from "@uniswap/v4-core/src/libraries/TickMath.sol";
import {SafeCast} from "@openzeppelin/contracts/utils/math/SafeCast.sol";
import {LiquidityAmounts} from "@uniswap/v4-periphery/src/libraries/LiquidityAmounts.sol";
import {TouchstoneVault} from "../../src/TouchstoneVault.sol";
import {Obs} from "../../src/types/Obs.sol";
import {Entry} from "../../src/types/Entry.sol";
import {Markout} from "../../src/types/Markout.sol";
import {SettleS14} from "./SettleS14.sol";
import {Op} from "../../src/types/Bank.sol";
import {Holdings} from "../../src/types/Holdings.sol";
import {Bank, Op, RING} from "../../src/types/Bank.sol";
import {FeeBank} from "../../src/libraries/FeeBank.sol";
import {ExecLedger} from "../../src/libraries/ExecLedger.sol";
import {FullMath} from "@uniswap/v4-core/src/libraries/FullMath.sol";
import {Tier} from "../../src/types/Tier.sol";
import {ITouchstoneFactory} from "../../src/interfaces/ITouchstoneFactory.sol";

/// @notice Test-only extension of the vault; it never ships. One contract, two deployments:
///         - scripted (behind `MockVaultFactory`, whose hook is `AckRecorder`): `checkpoint(o, liveOk, liveTick)` runs the
///           vault's `_checkpoint` on a scripted observation with no PoolManager, `_tryRef` returns the scripted read and
///           counts its calls, and setters write the state;
///         - through the real factory (the invariant handler): `_tryRef` is the production read and `placeForTest(lo, hi)`
///           places the maximum liquidity from idle inside the vault's own unlock, after the production prologue and
///           mode gate, then registers the range with the hook.
/// @notice The vault's scalar state, as the scenario vectors carry it.
struct Scalars {
    uint128 nav0;
    uint64 delta0;
    uint64 tauV0;
    uint40 segStart;
    uint128 baseOwed;
    uint32 epoch;
    uint64 epochSwapFrac;
    uint16 swapCount;
}

/// @notice The vault's `Checkpoint` event payload.
struct CheckpointLog {
    uint64 tauV;
    uint64 tauB;
    uint256 nav0;
    uint64 delta;
    uint256 base;
    uint128 L;
    int24 lo;
    int24 hi;
}

contract VaultHarness is TouchstoneVault {
    using StateLibrary for *;

    address internal constant OTHER_HOLDER = address(0xBEEF);

    bool internal _scripted;
    bool internal _liveOk;
    int24 internal _liveTick;
    uint256 public liveReads;
    /// @dev The harness keeps its own copy of the tier (the production vault keeps only `TIER_HASH`).
    Tier internal _tierCopy;

    constructor() {
        _tierCopy = ITouchstoneFactory(msg.sender).pending().tier;
    }

    function tier() external view returns (Tier memory) {
        return _tierCopy;
    }

    function holdings() external view returns (Holdings memory) {
        return _holdings();
    }

    function nav0() external view returns (uint256) {
        return _nav0;
    }

    function bondShares() external view returns (uint256) {
        return _bank.bondShares;
    }

    function bank() external view returns (uint128 cohortShares, uint128 mp, uint128 vu, uint128 bond, uint128 pool, uint8 mode, uint32 breaches) {
        Bank storage b = _bank;
        for (uint256 k; k < RING; ++k) {
            cohortShares += b.cohorts[k].shares;
        }
        return (cohortShares, b.maturedPending, b.vestedUnclaimed, b.bondShares, b.forfeitPool, b.mode, b.breaches);
    }

    // ------------------------------------------------------------------ placement seam (factory deployment)

    /// @dev Which test seam the next dispatch serves (0: none; 1: `placeForTest`; 2: `creditIdle`).
    uint8 internal _seam;

    /// @notice A placement with no guards, no charge and no pending record (test setup only; the curator's
    ///         `setPositions` is the production path). Runs the production prologue and mode gate.
    function placeForTest(int24 lo, int24 hi) external nonReentrant {
        _seam = 1;
        MANAGER.unlock(abi.encode(Entry.SetPositions, msg.sender, abi.encode(lo, hi)));
    }

    /// @notice Credits idle with `a0`/`a1` paid by the caller (claims minted, the ledger raised), with no shares: a
    ///         state seam for inventories the flows cannot reach directly (e.g. a 90% token0 vault).
    function creditIdle(uint256 a0, uint256 a1) external nonReentrant {
        _seam = 2;
        MANAGER.unlock(abi.encode(Entry.Settle, msg.sender, abi.encode(a0, a1)));
    }

    function _dispatch(Entry entry, address payer, bytes memory args, bool liveRead, int24 liveTick)
        internal
        override
        returns (bytes memory)
    {
        uint8 seam = _seam;
        _seam = 0;
        if (seam == 1) return _placeForTest(args);
        if (seam == 2) {
            (uint256 a0, uint256 a1) = abi.decode(args, (uint256, uint256));
            _payIn(C0, payer, a0);
            _payIn(C1, payer, a1);
            _mintClaims(C0, a0);
            _mintClaims(C1, a1);
            (_idle0, _idle1) = (SafeCast.toUint128(_idle0 + a0), SafeCast.toUint128(_idle1 + a1));
            return "";
        }
        return super._dispatch(entry, payer, args, liveRead, liveTick);
    }

    // ------------------------------------------------------------------ the vault's move seam

    /// @dev The exact output the vault's move requests (0: the production `type(int128).max`). A finite amount makes
    ///      the move fill before its price limit, the miss that only an astronomically deep pool reaches otherwise.
    int256 public moveOutOverride;

    function setMoveOut(int256 v) external {
        moveOutOverride = v;
    }

    function _moveOut() internal view override returns (int256) {
        return moveOutOverride != 0 ? moveOutOverride : super._moveOut();
    }

    function _placeForTest(bytes memory args) internal returns (bytes memory) {
        (int24 lo, int24 hi) = abi.decode(args, (int24, int24));
        _removeToIdle();
        (_lo, _hi) = (lo, hi);
        (uint160 sp,,,) = MANAGER.getSlot0(PoolId.wrap(keccak256(abi.encode(_key()))));
        uint128 L = LiquidityAmounts.getLiquidityForAmounts(
            sp, TickMath.getSqrtPriceAtTick(lo), TickMath.getSqrtPriceAtTick(hi), _idle0, _idle1
        );
        uint128 head = L / 1e9 + 1;
        L = L > head ? L - head : 0;
        if (L > 0) {
            BalanceDelta d = _modify(int256(uint256(L)));
            uint256 p0 = uint256(int256(-d.amount0()));
            uint256 p1 = uint256(int256(-d.amount1()));
            if (p0 > 0) MANAGER.burn(address(this), C0.toId(), p0);
            if (p1 > 0) MANAGER.burn(address(this), C1.toId(), p1);
            _idle0 -= uint128(p0);
            _idle1 -= uint128(p1);
            _L = L;
        }
        HOOK.setVaultRange(lo, hi, HOOK.liveRef());
        // the placement's mode row (NORMAL, placedByCurator), as setPositions applies it
        FeeBank.bankApply(_bank, Op.Place, "");
        return "";
    }

    // ------------------------------------------------------------------ positive-control seams (manipulation cells)

    /// @dev M-5b's control: the prologue does not collect first (the action's own removal then pays the accrued fees
    ///      into idle, inside the measured action).
    bool public noCollectFirst;
    /// @dev M-3's control: a fee-linked pay (s = 1): the fees collected first are paid to `feePayTo` as new shares,
    ///      sized so that its other holdings keep their share of the fees.
    address public feePayTo;
    /// @dev M-ADV3-move's controls: MU-239/240 (the vault's move held to neither swap cap) and MU-241 (the move not
    ///      counted in the epoch counter).
    bool public moveCapsOff;
    bool public moveCounterOff;

    function setControls(bool noCollect, address payTo, bool capsOff, bool counterOff) external {
        (noCollectFirst, feePayTo, moveCapsOff, moveCounterOff) = (noCollect, payTo, capsOff, counterOff);
    }

    function _prologue() internal override returns (Obs memory o) {
        if (noCollectFirst) return HOOK.snapshotIfNewBlock();
        if (feePayTo == address(0)) return super._prologue();
        (uint160 sp,,,) = MANAGER.getSlot0(POOL_ID);
        (uint256 nav0_,) = ExecLedger.value(_holdings(), sp);
        o = super._prologue();
        (uint256 nav1_,) = ExecLedger.value(_holdings(), sp);
        uint256 S = totalSupply();
        // the curator's holdings: its bank (bond, cohorts, matured, vested, reserve) and the shares it holds
        uint256 b = _bankHeld() + balanceOf(feePayTo);
        if (nav1_ > nav0_ && S > 0) {
            // (b + m)·P/(S + m) = b·P/S + F, with P = NAV after the fees and F the fees: m = F·S²/(P·S − b·P − F·S)
            uint256 f = nav1_ - nav0_;
            uint256 m = FullMath.mulDiv(f * S, S, nav1_ * S - b * nav1_ - f * S);
            if (m > 0) _mint(feePayTo, m);
        }
    }

    function _moveCap(uint256 moveFrac) internal override {
        if (moveCapsOff) {
            if (!moveCounterOff) _epochSwapFrac += uint64(moveFrac);
            return;
        }
        if (moveCounterOff) {
            if (moveFrac > MAX_SWAP_ACTION || _epochSwapFrac + moveFrac > MAX_SWAP_EPOCH) revert SwapCap();
            return;
        }
        super._moveCap(moveFrac);
    }

    // ------------------------------------------------------------------ pending action seam

    /// @dev The control of the settlement's gas sweep: with the guard off, a starved read takes the fallback.
    bool public gasGuardOff;

    function setGasGuardOff(bool off) external {
        gasGuardOff = off;
    }

    /// @dev The positive control of M-ADV3-move (F-044): the s14 settlement in place of the fee-aware one.
    bool internal _settleS14;

    function setSettleS14(bool on) external {
        _settleS14 = on;
    }

    /// @dev The production settlement, or (control) the s14 arithmetic on the same record, window read and bank op.
    function _settle() internal override {
        if (!_settleS14) return super._settle();
        uint256 tA = _tAction;
        if (tA == 0 || block.timestamp < tA + 2 * BLOCKTIME + H_MARKOUT) return;
        uint256 a = block.timestamp - tA - BLOCKTIME;
        (bool ok, int24 tickMk) = HOOK.tryWindow(uint32(a), uint32(a - H_MARKOUT));
        uint256 burnS = _bank.reserveShares;
        if (ok) {
            (, burnS) = SettleS14.settleExtra(
                _pBefore, _pAfter, tickMk, FEE_TICKS, _pend.charge0, _pend.reserve, _holdings(), totalSupply(), burnS
            );
        }
        _tAction = 0;
        _burnBank(Op.Settle, abi.encode(burnS, uint32(block.number / EPOCH), uint40(block.number)));
    }

    function _settleGasMin() internal view override returns (uint256) {
        return gasGuardOff ? 0 : super._settleGasMin();
    }

    /// @notice Writes a pending action's record (the reserve's shares are set through `setBank`).
    function setPending(uint40 tA, Holdings calldata before, Holdings calldata after_, uint128 charge0, uint128 reserve)
        external
    {
        (_tAction, _pBefore, _pAfter) = (tA, before, after_);
        // the fee terms' start spots at the current spot, no immediate burn, the live supply (`setMarkout` overrides)
        (uint160 sp,,,) = MANAGER.getSlot0(POOL_ID);
        _pend = Markout(charge0, reserve, sp, sp, 0, _u128(totalSupply()));
    }

    function setMarkout(Markout calldata m) external {
        _pend = m;
    }

    function markout() external view returns (Markout memory) {
        return _pend;
    }

    function feePips() external view returns (uint24) {
        return FEE;
    }

    function pendingAction()
        external
        view
        returns (uint40 tA, Holdings memory before, Holdings memory after_, uint128 charge0, uint128 reserve)
    {
        return (_tAction, _pBefore, _pAfter, _pend.charge0, _pend.reserve);
    }

    /// @notice The epoch record of `e` (spec §3.9).
    function record(uint32 e) external view returns (EpochRecord memory) {
        return _records[e];
    }

    /// @notice Test-only: overwrite the record of `e`.
    function setRecord(uint32 e, EpochRecord calldata r) external {
        _records[e] = r;
    }

    // ------------------------------------------------------------------ scripted checkpoint

    function checkpoint(Obs calldata o, bool liveOk, int24 liveTick) external returns (bool liveRead, int24 tick) {
        (_scripted, _liveOk, _liveTick, liveReads) = (true, liveOk, liveTick, 0);
        return _checkpoint(o);
    }

    function _tryRef() internal override returns (bool ok, int24 tick) {
        if (!_scripted) return super._tryRef();
        ++liveReads;
        return (_liveOk, _liveTick);
    }

    // ------------------------------------------------------------------ setters (scripted deployment)

    function setHoldings(Holdings calldata h) external {
        (_idle0, _idle1, _L, _lo, _hi) = (h.idle0, h.idle1, h.L, h.lo, h.hi);
    }

    function setBank(Bank calldata b) external {
        _bank = b;
    }

    /// @dev Mints the supply: the bank's shares to this vault (vault-held shares cover the bank), the rest elsewhere.
    function setSupply(uint256 S) external {
        uint256 held = _bankHeld();
        if (held > S) held = S;
        if (held > 0) _mint(address(this), held);
        if (S > held) _mint(OTHER_HOLDER, S - held);
    }

    function setScalars(
        uint128 nav0_,
        uint64 delta0_,
        uint64 tauV0_,
        uint40 segStart_,
        uint128 baseOwed_,
        uint32 epoch_,
        uint64 epochSwapFrac_,
        uint16 swapCount_
    ) external {
        (_nav0, _delta0, _tauV0, _segStart, _baseOwed) = (nav0_, delta0_, tauV0_, segStart_, baseOwed_);
        (_epoch, _epochSwapFrac, _swapCount) = (epoch_, epochSwapFrac_, swapCount_);
    }

    // ------------------------------------------------------------------ getters

    function bankFull() external view returns (Bank memory) {
        return _bank;
    }

    /// @dev Sends an arbitrary entry through the vault's own unlock (the dispatch's unhandled-entry test).
    function rawEntry(Entry entry) external {
        MANAGER.unlock(abi.encode(entry, msg.sender, abi.encode(uint256(1), uint256(0), uint256(0), msg.sender)));
    }

    function lastAction() external view returns (uint40) {
        return _lastAction;
    }

    function setLastAction(uint40 b) external {
        _lastAction = b;
    }

    function scalars() external view returns (Scalars memory s) {
        return Scalars(_nav0, _delta0, _tauV0, _segStart, _baseOwed, _epoch, _epochSwapFrac, _swapCount);
    }

    function _bankHeld() internal view returns (uint256 u) {
        Bank storage b = _bank;
        for (uint256 k; k < RING; ++k) {
            u += b.cohorts[k].shares;
        }
        u += uint256(b.maturedPending) + b.vestedUnclaimed + b.bondShares + b.forfeitPool + b.reserveShares;
    }
}
