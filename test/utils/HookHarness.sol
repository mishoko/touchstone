// SPDX-License-Identifier: MIT
pragma solidity 0.8.26;

import {TouchstoneHook} from "../../src/TouchstoneHook.sol";
import {HookArgs} from "../../src/interfaces/ITouchstoneFactory.sol";

/// @notice The hook with a scripted pool price (`_slot0` seam), a witness-range setter, the internal `_refresh`
///         exposed, and getters for the state the golden vectors check.
contract HookHarness is TouchstoneHook {
    uint160 internal _sqrtScript;
    int24 internal _tickScript;

    constructor(HookArgs memory a) TouchstoneHook(a) {}

    function setSlot0(uint160 sqrtP, int24 tick) external {
        (_sqrtScript, _tickScript) = (sqrtP, tick);
    }

    function _slot0() internal view override returns (uint160, int24) {
        return (_sqrtScript, _tickScript);
    }

    function setWitnessRange(int24 lo, int24 hi) external {
        (_witLo, _witHi) = (lo, hi);
    }

    function exposedRefresh() external {
        _refresh();
    }

    function state()
        external
        view
        returns (uint32 refGrid, uint40 lastSnap, bool armed, int24 vaultLo, int24 vaultHi, int24 epochStartRef_)
    {
        return (_refGrid, _lastSnapBlock, _armed, _vaultLo, _vaultHi, _epochStartRef);
    }

    function frozenSlot(uint256 i) external view returns (Frozen memory) {
        return _frozen[i];
    }

    function witness() external view returns (int24 lo, int24 hi, uint32 epoch, uint128 l, uint128 r0, uint128 r1) {
        return (_witLo, _witHi, _witEpoch, _witL, _witnessReserve0, _witnessReserve1);
    }

    function witnessFallback() external view returns (bool) {
        return _witnessFallback;
    }

    function ringAt(uint256 i) external view returns (uint64) {
        return _ring[i];
    }

    function ringHeadCount() external view returns (uint8 head, uint8 count) {
        return (_head, _count);
    }
}

/// @notice The production hook (real pool price) with the witness range settable and the ring and state readable,
///         for tests on a live PoolManager pool.
contract HookExposed is TouchstoneHook {
    constructor(HookArgs memory a) TouchstoneHook(a) {}

    function setWitnessRange(int24 lo, int24 hi) external {
        (_witLo, _witHi) = (lo, hi);
    }

    function ringAt(uint256 i) external view returns (uint64) {
        return _ring[i];
    }

    function ringHeadCount() external view returns (uint8 head, uint8 count) {
        return (_head, _count);
    }

    function witness() external view returns (int24 lo, int24 hi, uint32 epoch, uint128 l, uint128 r0, uint128 r1) {
        return (_witLo, _witHi, _witEpoch, _witL, _witnessReserve0, _witnessReserve1);
    }

    function witnessFallback() external view returns (bool) {
        return _witnessFallback;
    }
}
