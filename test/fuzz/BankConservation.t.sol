// SPDX-License-Identifier: MIT
pragma solidity 0.8.26;

import {Test} from "forge-std/Test.sol";
import {FeeBank} from "../../src/libraries/FeeBank.sol";
import {Bank, Op, RING} from "../../src/types/Bank.sol";
import {Ladder} from "../../src/libraries/Ladder.sol";

/// @notice F-3: bank conservation. Random op sequences on `FeeBank.bankApply`; after every op the change in the sum of
///         the buckets (cohorts, maturedPending, vestedUnclaimed, bond, forfeit pool and the reserve) equals the
///         shares minted (Accrue) plus the shares topped up, minus the shares the vault must burn and pay.
contract BankConservationTest is Test {
    Bank internal b;
    uint40 internal now_;
    uint32 internal epoch;

    function applyExt(Op op, bytes calldata args) external returns (uint256, uint256, uint256, uint256) {
        return FeeBank.bankApply(b, op, args);
    }

    function _sum() internal view returns (uint256 s) {
        for (uint256 k; k < RING; ++k) {
            s += b.cohorts[k].shares;
        }
        s += uint256(b.maturedPending) + b.vestedUnclaimed + b.bondShares + b.forfeitPool + b.reserveShares;
    }

    /// forge-config: default.fuzz.runs = 64
    function test_F_3__bankConservation(uint256 seed) public {
        b.bondShares = 1e24 - 1e15;
        b.dripBlocks = 120;
        b.releaseDelay = 480;
        now_ = 1000;
        epoch = 8;
        uint256 executed;
        for (uint256 i; i < 40; ++i) {
            seed = uint256(keccak256(abi.encode(seed, i)));
            (Op op, bytes memory args, uint256 added) = _pick(seed);
            uint256 before = _sum();
            try this.applyExt(op, args) returns (uint256 burnShares, uint256 payShares, uint256, uint256) {
                assertEq(_sum() + burnShares + payShares, before + added, "conservation");
                ++executed;
            } catch (bytes memory err) {
                // a revert must be one of the bank's own guards (never a panic or an arithmetic failure); value review
                // V-2 (F-043): the previous check, that a reverted call changes nothing, held by the EVM's semantics
                bytes4 sel = bytes4(err);
                assertTrue(
                    sel == FeeBank.Insolvent.selector || sel == FeeBank.MarkoutPending.selector
                        || sel == FeeBank.BondLocked.selector || sel == Ladder.BadMode.selector,
                    "a revert outside the bank's guards"
                );
            }
            assertLe(b.reserveBond, b.reserveShares, "reserve bond part");
            assertLe(b.forfeitPool, b.forfeitTotal, "drip target");
            // time moves forward
            uint256 dt = (seed >> 200) % 90;
            now_ += uint40(dt);
            epoch = uint32(now_ / 120);
        }
        assertGt(executed, 0, "non-vacuous");
    }

    function _pick(uint256 r) internal view returns (Op op, bytes memory args, uint256 added) {
        uint256 amt = (r >> 16) % 1e24;
        bool gate = (r >> 128) & 1 == 1;
        uint256 k = r % 11;
        if (k == 0) return (Op.Accrue, abi.encode(epoch, amt, gate), amt);
        if (k == 1) return (Op.Breach, abi.encode(uint32((r >> 96) % 3), now_), 0);
        if (k == 2) return (Op.VestTime, abi.encode(epoch, gate), 0);
        if (k == 3) return (Op.Drip, abi.encode(now_), 0);
        if (k == 4) return (Op.Charge, abi.encode(amt / 4, (r >> 160) % 2 == 0 ? 0 : amt / 8), 0);
        if (k == 5) return (Op.Place, "", 0);
        if (k == 6) return (Op.Retire, abi.encode(now_), 0);
        if (k == 7) return (Op.Claim, "", 0);
        if (k == 8) return (Op.TopUp, abi.encode(amt), amt);
        if (k == 9) return (Op.Release, abi.encode(amt, now_), 0);
        return (Op.Settle, abi.encode(amt / 16, epoch, now_), 0);
    }
}
