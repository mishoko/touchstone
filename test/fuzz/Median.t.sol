// SPDX-License-Identifier: MIT
pragma solidity 0.8.26;

import {MedianOracle} from "../../src/libraries/MedianOracle.sol";
import {HookBase} from "../utils/HookBase.sol";

contract MedianFuzzTest is HookBase {
    function setUp() public {
        _setUpHook(false);
        refConst(0);
    }

    function _sorted(int24[] memory a) internal pure returns (int24[] memory b) {
        b = new int24[](a.length);
        for (uint256 i; i < a.length; ++i) {
            b[i] = a[i];
        }
        for (uint256 i; i < b.length; ++i) {
            for (uint256 j = i + 1; j < b.length; ++j) {
                if (b[j] < b[i]) (b[i], b[j]) = (b[j], b[i]);
            }
        }
    }

    /// The median equals the lower-middle element of a full sort, for 1 to 9 values.
    function test_F_2__medianEqualsSort(int24[9] memory v, uint8 n) public pure {
        uint256 len = bound(n, 1, 9);
        int24[] memory a = new int24[](len);
        for (uint256 i; i < len; ++i) {
            a[i] = v[i];
        }
        int24 want = _sorted(a)[(len - 1) / 2];
        assertEq(MedianOracle.median(a), want);
    }

    /// The ring's med9 equals the sorted median of the last min(count, 9) block-start ticks, across wraps.
    function test_F_2__ringMed9EqualsSortOfTheLastNine(int24[40] memory v, uint8 n) public {
        uint256 len = bound(n, 1, 40);
        for (uint256 i; i < len; ++i) {
            int24 t = int24(bound(v[i], -887272, 887271));
            v[i] = t;
            pool(t);
            snap();
            advance(1);
        }
        uint256 k = len < 9 ? len : 9;
        int24[] memory last = new int24[](k);
        for (uint256 i; i < k; ++i) {
            last[i] = v[len - 1 - i];
        }
        assertEq(h.obs().med9, _sorted(last)[(k - 1) / 2]);
    }
}
