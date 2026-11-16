// SPDX-License-Identifier: MIT
pragma solidity 0.8.26;

import {IPoolManager} from "@uniswap/v4-core/src/interfaces/IPoolManager.sol";
import {StateLibrary} from "@uniswap/v4-core/src/libraries/StateLibrary.sol";
import {LiquidPoolBase} from "../utils/LiquidPoolBase.sol";
import {VaultHandler} from "./handlers/VaultHandler.sol";
import {Holdings} from "../../src/types/Holdings.sol";
import {Ladder} from "../../src/libraries/Ladder.sol";

/// @notice The P2 invariants on the minimal handler with a live position (L > 0 from the start):
///         I-1 the vault's ERC-6909 claims and position cover its ledger, and the hook's claims cover its witness
///         reserve; I-2 every withdrawal pays exactly the pro-rata share (idle legs floored, the L leg the
///         PoolManager's rounded-down amounts for floor(L·s/S_pre)); I-10 a withdrawal never reverts outside the
///         allow-list, with the reference broken in every mode.
abstract contract VaultP2Invariants is LiquidPoolBase {
    using StateLibrary for IPoolManager;

    VaultHandler internal handler;

    function setUp() public virtual override {
        super.setUp();
        int24 c = _seedTick();
        hv.placeForTest(c - 600, c + 600);
        (uint256 m0, uint256 m1) = _highTick() ? (uint256(30_000e6), uint256(10e18)) : (uint256(1e20), uint256(1e20));
        handler = new VaultHandler(
            IPoolManager(address(manager)), hv, hook, refPool, token0, token1, key, blocktime, c, m0, m1
        );
        targetContract(address(handler));
    }

    function invariant_I_1__claimsAndPositionCoverTheLedger() public view {
        Holdings memory h = hv.holdings();
        IPoolManager m = IPoolManager(address(manager));
        assertGe(m.balanceOf(vault, uint256(uint160(address(token0)))), h.idle0, "vault claims0 >= idle0");
        assertGe(m.balanceOf(vault, uint256(uint160(address(token1)))), h.idle1, "vault claims1 >= idle1");
        (uint128 liq,,) = m.getPositionInfo(poolId, vault, h.lo, h.hi, bytes32(0));
        assertGe(liq, h.L, "position >= L");
        // the hook's witness reserve (slot 4: _witnessReserve0 | _witnessReserve1 << 128)
        uint256 w = uint256(vm.load(hook, bytes32(uint256(4))));
        assertGe(m.balanceOf(hook, uint256(uint160(address(token0)))), uint128(w), "hook claims0 >= reserve0");
        assertGe(m.balanceOf(hook, uint256(uint160(address(token1)))), w >> 128, "hook claims1 >= reserve1");
    }

    function invariant_I_2__withdrawPaysTheExactProRataShare() public view {
        assertEq(handler.i2Failures(), 0, handler.i2Why());
    }

    function invariant_I_10__withdrawNeverRevertsOutsideTheAllowList() public view {
        assertEq(handler.i10Failures(), 0, vm.toString(abi.encodePacked(handler.i10Selector())));
    }

    /// @notice Non-vacuity of the handler: one fixed sequence reaches every state the invariants must see (L > 0
    ///         withdrawals with fees, a broken reference in every mode, breaches, EXITED) with no I-2 or I-10 failure.
    function test_I_10__handlerSequenceReachesEveryStateWithoutFailures() public {
        address a = address(uint160(0xA11CE0));
        for (uint256 i; i < 3; ++i) handler.deposit(i, 5e22);
        for (uint256 i; i < 6; ++i) {
            handler.swap(i % 2 == 0, type(uint256).max, 300);
            handler.advance(1);
            handler.withdraw(i, 1e21 + i);
        }
        for (uint256 m = 5; m < 10; ++m) {
            handler.breakRef(m);
            handler.advance(99 * 256 + 99); // a silent jump
            handler.withdraw(m, 3e20 + m);
            handler.deposit(m, 1e20);
        }
        handler.breakRef(0);
        handler.refresh();
        for (uint256 i; i < 3; ++i) handler.driveBreach();
        handler.withdraw(0, hv.balanceOf(a) / 2);
        (,,,,, uint8 mode,) = v.bank();
        assertEq(mode, Ladder.EXITED, "EXITED reached");
        assertGt(handler.withdrawalsWithL(), 10, "withdrawals with L > 0");
        assertGe(handler.breachesDriven(), 3, "three breaches driven");
        assertGt(handler.exitedWithdrawals(), 0, "a withdrawal in EXITED");
        assertEq(handler.i2Failures(), 0, handler.i2Why());
        assertEq(handler.i10Failures(), 0, "I-10");
    }
}

contract VaultP2InvariantsTick0 is VaultP2Invariants {}

contract VaultP2InvariantsTick196k is VaultP2Invariants {
    function _highTick() internal pure override returns (bool) {
        return true;
    }
}
