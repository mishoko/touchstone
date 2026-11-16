// SPDX-License-Identifier: MIT
pragma solidity 0.8.26;

import {VectorBase} from "../utils/VectorBase.sol";
import {Ladder} from "../../src/libraries/Ladder.sol";

/// @notice Replays test/vectors/mode.json against `Ladder.next` and `Ladder.allowed`, bit for bit.
contract ModeVectorsTest is VectorBase {
    /// @dev External wrapper so a `BadMode` revert of the internal library can be caught.
    function next(uint8 mode, uint32 breaches, uint8 ev, uint32 count) external pure returns (uint8, uint32, bool) {
        return Ladder.next(mode, breaches, ev, count);
    }

    function test_U_ladder__modeVectorsBitForBit() public {
        string[] memory cs = _cases("mode");
        uint256 nNext;
        uint256 nAllowed;
        for (uint256 i; i < cs.length; ++i) {
            string memory c = cs[i];
            _keys(c, "$", _l4("id", "fn", "in", "out"));
            cid = _s(c, ".id");
            string memory fn = _s(c, ".fn");
            if (_eq(fn, "Ladder.allowed")) {
                _keys(c, ".in", _l2("mode", "entry"));
                _keys(c, ".out", _l1("allowed"));
                bool got = Ladder.allowed(uint8(_u(c, ".in.mode")), uint8(_u(c, ".in.entry")));
                assertEq(got, _b(c, ".out.allowed"), cid);
                ++nAllowed;
            } else {
                assertEq(fn, "Ladder.next", cid);
                if (_has(c, ".in", "events")) _runSeq(c);
                else _runSingle(c);
                ++nNext;
            }
        }
        assertEq(nNext + nAllowed, cs.length, "cases");
        assertEq(nAllowed, 36, "every (mode x entry) pair");
    }

    function _runSingle(string memory c) internal view {
        _keys(c, ".in", _l4("mode", "breaches", "ev", "count"));
        _step(
            c,
            ".out",
            uint8(_u(c, ".in.mode")),
            uint32(_u(c, ".in.breaches")),
            uint8(_u(c, ".in.ev")),
            uint32(_u(c, ".in.count"))
        );
    }

    function _runSeq(string memory c) internal view {
        _keys(c, ".in", _l3("mode", "breaches", "events"));
        _keys(c, ".out", _l1("steps"));
        uint8 mode = uint8(_u(c, ".in.mode"));
        uint32 breaches = uint32(_u(c, ".in.breaches"));
        uint256 n;
        while (vm.keyExistsJson(c, _p(".in.events[", n, "]"))) ++n;
        assertGt(n, 0, cid);
        for (uint256 k; k < n; ++k) {
            string memory e = _p(".in.events[", k, "]");
            _keys(c, e, _l2("ev", "count"));
            (mode, breaches) = _step(
                c,
                _p(".out.steps[", k, "]"),
                mode,
                breaches,
                uint8(_u(c, string.concat(e, ".ev"))),
                uint32(_u(c, string.concat(e, ".count")))
            );
        }
    }

    /// @return the state after the step (unchanged on a revert)
    function _step(string memory c, string memory o, uint8 mode, uint32 breaches, uint8 ev, uint32 count)
        internal
        view
        returns (uint8, uint32)
    {
        try this.next(mode, breaches, ev, count) returns (uint8 m, uint32 b, bool x) {
            _keys(c, o, _l3("mode", "breaches", "exited"));
            assertEq(m, _u(c, string.concat(o, ".mode")), string.concat(cid, " mode"));
            assertEq(b, _u(c, string.concat(o, ".breaches")), string.concat(cid, " breaches"));
            assertEq(x, _b(c, string.concat(o, ".exited")), string.concat(cid, " exited"));
            return (m, b);
        } catch (bytes memory err) {
            _keys(c, o, _l1("revert"));
            assertEq(_errName(err, _l1("BadMode")), _s(c, string.concat(o, ".revert")), cid);
            return (mode, breaches);
        }
    }
}
