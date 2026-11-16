// SPDX-License-Identifier: MIT
pragma solidity 0.8.26;

import {BalanceDelta} from "@uniswap/v4-core/src/types/BalanceDelta.sol";
import {PoolId} from "@uniswap/v4-core/src/types/PoolId.sol";
import {StateLibrary} from "@uniswap/v4-core/src/libraries/StateLibrary.sol";
import {TickMath} from "@uniswap/v4-core/src/libraries/TickMath.sol";
import {SafeCast} from "@openzeppelin/contracts/utils/math/SafeCast.sol";
import {LiquidityAmounts} from "@uniswap/v4-periphery/src/libraries/LiquidityAmounts.sol";
import {TouchstoneVault} from "../../src/TouchstoneVault.sol";
import {Obs} from "../../src/types/Obs.sol";
import {Entry} from "../../src/types/Entry.sol";
import {Holdings} from "../../src/types/Holdings.sol";
import {Bank, Op, RING} from "../../src/types/Bank.sol";
import {FeeBank} from "../../src/libraries/FeeBank.sol";
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

    function placeForTest(int24 lo, int24 hi) external nonReentrant {
        MANAGER.unlock(abi.encode(Entry.SetPositions, msg.sender, abi.encode(lo, hi)));
    }

    function _dispatch(Entry entry, address payer, bytes memory args) internal override returns (bytes memory) {
        if (entry == Entry.SetPositions) return _placeForTest(args);
        return super._dispatch(entry, payer, args);
    }

    function _placeForTest(bytes memory args) internal returns (bytes memory) {
        (int24 lo, int24 hi) = abi.decode(args, (int24, int24));
        if (_L > 0) {
            BalanceDelta r = _modify(-int256(uint256(_L)));
            uint256 r0 = uint256(int256(r.amount0()));
            uint256 r1 = uint256(int256(r.amount1()));
            _mintClaims(C0, r0);
            _mintClaims(C1, r1);
            _idle0 = SafeCast.toUint128(_idle0 + r0);
            _idle1 = SafeCast.toUint128(_idle1 + r1);
            _L = 0;
        }
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
