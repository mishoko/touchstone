// SPDX-License-Identifier: MIT
pragma solidity 0.8.26;

import {Test} from "forge-std/Test.sol";
import {JsonSlice} from "./JsonSlice.sol";

/// @notice Shared helpers for the golden-vector runners: the file header check, strict key sets (an unknown or missing
///         field fails the run) and decimal-string integer parsing.
abstract contract VectorBase is Test {
    string internal cid; // the current case id, for messages

    /// @notice Reads `test/vectors/<name>.json`, checks its header and returns its cases, one JSON string each.
    function _cases(string memory name) internal returns (string[] memory cs) {
        string memory json = vm.readFile(string.concat("test/vectors/", name, ".json"));
        cid = name;
        _keys(json, "$", _l4("schema", "modelHash", "conventions", "cases"));
        assertEq(vm.parseJsonString(json, ".schema"), "touchstone-vectors/1", "schema");
        cs = JsonSlice.elements(json, "cases");
        assertGt(cs.length, 0, "no cases");
    }

    function _keys(string memory c, string memory path, string[] memory want) internal view {
        string[] memory got = vm.parseJsonKeys(c, path);
        for (uint256 i; i < got.length; ++i) {
            bool known;
            for (uint256 x; x < want.length; ++x) {
                if (keccak256(bytes(got[i])) == keccak256(bytes(want[x]))) known = true;
            }
            assertTrue(known, string.concat(cid, ": unknown field ", got[i], " at ", path));
        }
        assertEq(got.length, want.length, string.concat(cid, ": missing field at ", path));
    }

    function _has(string memory c, string memory path, string memory key) internal view returns (bool) {
        string[] memory got = vm.parseJsonKeys(c, path);
        for (uint256 i; i < got.length; ++i) {
            if (keccak256(bytes(got[i])) == keccak256(bytes(key))) return true;
        }
        return false;
    }

    function _u(string memory c, string memory path) internal pure returns (uint256) {
        return vm.parseUint(vm.parseJsonString(c, path));
    }

    function _i(string memory c, string memory path) internal pure returns (int256) {
        return vm.parseInt(vm.parseJsonString(c, path));
    }

    function _b(string memory c, string memory path) internal pure returns (bool) {
        return vm.parseJsonBool(c, path);
    }

    function _s(string memory c, string memory path) internal pure returns (string memory) {
        return vm.parseJsonString(c, path);
    }

    function _p(string memory a, uint256 i, string memory b) internal pure returns (string memory) {
        return string.concat(a, vm.toString(i), b);
    }

    function _eq(string memory a, string memory b) internal pure returns (bool) {
        return keccak256(bytes(a)) == keccak256(bytes(b));
    }

    /// @notice The custom-error name for a 4-byte selector, from a candidate list; reverts if unknown.
    function _errName(bytes memory data, string[] memory names) internal pure returns (string memory) {
        require(data.length >= 4, "revert without a selector");
        bytes4 sel = bytes4(data);
        for (uint256 i; i < names.length; ++i) {
            if (bytes4(keccak256(bytes(string.concat(names[i], "()")))) == sel) return names[i];
        }
        revert("unknown error selector");
    }

    function _l1(string memory a) internal pure returns (string[] memory l) {
        l = new string[](1);
        l[0] = a;
    }

    function _l2(string memory a, string memory b) internal pure returns (string[] memory l) {
        l = new string[](2);
        (l[0], l[1]) = (a, b);
    }

    function _l3(string memory a, string memory b, string memory c) internal pure returns (string[] memory l) {
        l = new string[](3);
        (l[0], l[1], l[2]) = (a, b, c);
    }

    function _l4(string memory a, string memory b, string memory c, string memory d)
        internal
        pure
        returns (string[] memory l)
    {
        l = new string[](4);
        (l[0], l[1], l[2], l[3]) = (a, b, c, d);
    }

    function _l5(string memory a, string memory b, string memory c, string memory d, string memory e)
        internal
        pure
        returns (string[] memory l)
    {
        l = new string[](5);
        (l[0], l[1], l[2], l[3], l[4]) = (a, b, c, d, e);
    }
}
