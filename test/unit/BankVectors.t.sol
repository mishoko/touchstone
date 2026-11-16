// SPDX-License-Identifier: MIT
pragma solidity 0.8.26;

import {VectorBase} from "../utils/VectorBase.sol";
import {FeeBank} from "../../src/libraries/FeeBank.sol";
import {Bank, Cohort, Op, RING} from "../../src/types/Bank.sol";

/// @notice Replays test/vectors/bank.json against the linked `FeeBank.bankApply` (the spec's `FeeBank.apply`; `apply` is reserved in Solidity), bit for bit: the bank before is written
///         to storage, the op is applied by `delegatecall`, and the bank after and the four returns are compared.
contract BankVectorsTest is VectorBase {
    Bank internal b;

    /// @dev External wrapper so a revert of the library call can be caught (and its writes undone).
    function applyExt(Op op, bytes calldata args) external returns (uint256, uint256, uint256, uint256) {
        return FeeBank.bankApply(b, op, args);
    }

    function test_U_4__bankVectorsBitForBit() public {
        string[] memory cs = _cases("bank");
        assertGe(cs.length, 30, "minimum bank cases");
        for (uint256 i; i < cs.length; ++i) {
            _runCase(cs[i]);
        }
    }

    function _runCase(string memory c) internal {
        _keys(c, "$", _l4("id", "fn", "in", "out"));
        cid = _s(c, ".id");
        assertEq(_s(c, ".fn"), "FeeBank.apply", cid);
        _keys(c, ".in", _l3("bank", "op", "args"));
        _writeBank(c, ".in.bank");
        Op op = Op(uint8(_u(c, ".in.op")));
        bytes memory args = _args(c, op);
        try this.applyExt(op, args) returns (uint256 burnShares, uint256 payShares, uint256 a, uint256 b2) {
            _keys(c, ".out", _l5("bank", "burnShares", "payShares", "a", "b2"));
            assertEq(burnShares, _u(c, ".out.burnShares"), string.concat(cid, " burnShares"));
            assertEq(payShares, _u(c, ".out.payShares"), string.concat(cid, " payShares"));
            assertEq(a, _u(c, ".out.a"), string.concat(cid, " a"));
            assertEq(b2, _u(c, ".out.b2"), string.concat(cid, " b2"));
            _checkBank(c, ".out.bank");
        } catch (bytes memory err) {
            _keys(c, ".out", _l1("revert"));
            string[] memory names = new string[](4);
            (names[0], names[1], names[2], names[3]) = ("Insolvent", "MarkoutPending", "BadMode", "BondLocked");
            assertEq(_errName(err, names), _s(c, ".out.revert"), cid);
            _checkBank(c, ".in.bank"); // a revert leaves the bank unchanged
        }
    }

    function _args(string memory c, Op op) internal view returns (bytes memory) {
        string memory a = ".in.args";
        if (op == Op.Accrue) {
            _keys(c, a, _l3("epoch", "shares", "gateOpen"));
            return abi.encode(uint32(_u(c, ".in.args.epoch")), _u(c, ".in.args.shares"), _b(c, ".in.args.gateOpen"));
        }
        if (op == Op.Breach) {
            _keys(c, a, _l2("count", "now"));
            return abi.encode(uint32(_u(c, ".in.args.count")), uint40(_u(c, ".in.args.now")));
        }
        if (op == Op.VestTime) {
            _keys(c, a, _l2("nowEpoch", "gateOpen"));
            return abi.encode(uint32(_u(c, ".in.args.nowEpoch")), _b(c, ".in.args.gateOpen"));
        }
        if (op == Op.Drip || op == Op.Retire) {
            _keys(c, a, _l1("now"));
            return abi.encode(uint40(_u(c, ".in.args.now")));
        }
        if (op == Op.Charge) {
            _keys(c, a, _l2("burn", "burnR"));
            return abi.encode(_u(c, ".in.args.burn"), _u(c, ".in.args.burnR"));
        }
        if (op == Op.TopUp) {
            _keys(c, a, _l1("shares"));
            return abi.encode(_u(c, ".in.args.shares"));
        }
        if (op == Op.Release) {
            _keys(c, a, _l2("shares", "now"));
            return abi.encode(_u(c, ".in.args.shares"), uint40(_u(c, ".in.args.now")));
        }
        if (op == Op.Settle) {
            _keys(c, a, _l3("burnS", "epoch", "now"));
            return abi.encode(_u(c, ".in.args.burnS"), uint32(_u(c, ".in.args.epoch")), uint40(_u(c, ".in.args.now")));
        }
        // Place, Claim: no arguments
        assertEq(vm.parseJsonKeys(c, a).length, 0, string.concat(cid, ": args"));
        return "";
    }

    function _fields() internal pure returns (string[] memory f) {
        f = new string[](16);
        f[0] = "cohorts";
        f[1] = "maturedPending";
        f[2] = "vestedUnclaimed";
        f[3] = "bondShares";
        f[4] = "forfeitPool";
        f[5] = "forfeitTotal";
        f[6] = "forfeitStart";
        f[7] = "dripBlocks";
        f[8] = "releaseDelay";
        f[9] = "breaches";
        f[10] = "mode";
        f[11] = "placedByCurator";
        f[12] = "exitBlock";
        f[13] = "reserveShares";
        f[14] = "reserveBond";
        f[15] = "reserveForfeit";
    }

    function _writeBank(string memory c, string memory p) internal {
        _keys(c, p, _fields());
        for (uint256 k; k < RING; ++k) {
            string memory e = string.concat(p, _p(".cohorts[", k, "]"));
            _keys(c, e, _l2("epoch", "shares"));
            b.cohorts[k] = Cohort(uint32(_u(c, string.concat(e, ".epoch"))), uint128(_u(c, string.concat(e, ".shares"))));
        }
        assertFalse(vm.keyExistsJson(c, string.concat(p, _p(".cohorts[", RING, "]"))), "cohort ring size");
        b.maturedPending = uint128(_u(c, string.concat(p, ".maturedPending")));
        b.vestedUnclaimed = uint128(_u(c, string.concat(p, ".vestedUnclaimed")));
        b.bondShares = uint128(_u(c, string.concat(p, ".bondShares")));
        b.forfeitPool = uint128(_u(c, string.concat(p, ".forfeitPool")));
        b.forfeitTotal = uint128(_u(c, string.concat(p, ".forfeitTotal")));
        b.forfeitStart = uint40(_u(c, string.concat(p, ".forfeitStart")));
        b.dripBlocks = uint40(_u(c, string.concat(p, ".dripBlocks")));
        b.releaseDelay = uint40(_u(c, string.concat(p, ".releaseDelay")));
        b.breaches = uint32(_u(c, string.concat(p, ".breaches")));
        b.mode = uint8(_u(c, string.concat(p, ".mode")));
        b.placedByCurator = _b(c, string.concat(p, ".placedByCurator"));
        b.exitBlock = uint40(_u(c, string.concat(p, ".exitBlock")));
        b.reserveShares = uint128(_u(c, string.concat(p, ".reserveShares")));
        b.reserveBond = uint128(_u(c, string.concat(p, ".reserveBond")));
        b.reserveForfeit = _b(c, string.concat(p, ".reserveForfeit"));
    }

    function _checkBank(string memory c, string memory p) internal view {
        _keys(c, p, _fields());
        for (uint256 k; k < RING; ++k) {
            string memory e = string.concat(p, _p(".cohorts[", k, "]"));
            assertEq(b.cohorts[k].epoch, _u(c, string.concat(e, ".epoch")), string.concat(cid, _p(" cohort ", k, " epoch")));
            assertEq(b.cohorts[k].shares, _u(c, string.concat(e, ".shares")), string.concat(cid, _p(" cohort ", k, " shares")));
        }
        _ck(c, p, ".maturedPending", b.maturedPending);
        _ck(c, p, ".vestedUnclaimed", b.vestedUnclaimed);
        _ck(c, p, ".bondShares", b.bondShares);
        _ck(c, p, ".forfeitPool", b.forfeitPool);
        _ck(c, p, ".forfeitTotal", b.forfeitTotal);
        _ck(c, p, ".forfeitStart", b.forfeitStart);
        _ck(c, p, ".dripBlocks", b.dripBlocks);
        _ck(c, p, ".releaseDelay", b.releaseDelay);
        _ck(c, p, ".breaches", b.breaches);
        _ck(c, p, ".mode", b.mode);
        assertEq(b.placedByCurator, _b(c, string.concat(p, ".placedByCurator")), string.concat(cid, " placedByCurator"));
        _ck(c, p, ".exitBlock", b.exitBlock);
        _ck(c, p, ".reserveShares", b.reserveShares);
        _ck(c, p, ".reserveBond", b.reserveBond);
        assertEq(b.reserveForfeit, _b(c, string.concat(p, ".reserveForfeit")), string.concat(cid, " reserveForfeit"));
    }

    function _ck(string memory c, string memory p, string memory f, uint256 got) internal view {
        assertEq(got, _u(c, string.concat(p, f)), string.concat(cid, f));
    }
}
