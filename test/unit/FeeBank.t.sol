// SPDX-License-Identifier: MIT
pragma solidity 0.8.26;

import {Test} from "forge-std/Test.sol";
import {FeeBank} from "../../src/libraries/FeeBank.sol";
import {Bank, Op} from "../../src/types/Bank.sol";

/// @notice FeeBank properties outside the golden vectors.
contract FeeBankTest is Test {
    Bank internal b;

    /// @notice The library moves storage only by `delegatecall`: a direct call to its address reverts and writes
    ///         nothing (it would otherwise operate on the library's own storage).
    function test_U_4__directCallToTheLibraryReverts() public {
        address lib = address(FeeBank);
        assertGt(lib.code.length, 0, "linked library deployed");
        bytes memory data = abi.encodeWithSelector(FeeBank.bankApply.selector, uint256(0), uint8(Op.Claim), bytes(""));
        (bool ok,) = lib.call(data);
        assertFalse(ok, "direct call");
        // control: the same op by delegatecall (the linked call) succeeds
        (uint256 burnShares, uint256 payShares,,) = FeeBank.bankApply(b, Op.Claim, "");
        assertEq(burnShares + payShares, 0);
    }

    /// @notice A charge larger than the unvested pay burns the bond next; vested-unclaimed shares are never burned.
    function test_U_4__chargeNeverBurnsVestedShares() public {
        b.cohorts[2].epoch = 7;
        b.cohorts[2].shares = 5;
        b.maturedPending = 10;
        b.vestedUnclaimed = 100;
        b.bondShares = 1000;
        (uint256 burnShares,, uint256 fromBank, uint256 fromBond) = FeeBank.bankApply(b, Op.Charge, abi.encode(uint256(50), uint256(0)));
        assertEq(burnShares, 50);
        assertEq(fromBank, 15, "cohort, then maturedPending");
        assertEq(fromBond, 35, "then the bond");
        assertEq(b.vestedUnclaimed, 100, "vested shares untouched");
        assertEq(b.bondShares, 965);
        assertEq(uint256(b.cohorts[2].shares) + b.maturedPending, 0);
    }
}
