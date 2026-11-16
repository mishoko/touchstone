// SPDX-License-Identifier: MIT
pragma solidity 0.8.26;

/// @notice Splits a top-level JSON array of objects into one string per element, so that a large vector file is
///         parsed once instead of once per field. Only the array under `"<key>"` of the top-level object is split.
library JsonSlice {
    function elements(string memory json, string memory key) internal pure returns (string[] memory out) {
        bytes memory j = bytes(json);
        bytes memory pat = abi.encodePacked('"', key, '"');
        uint256 n = j.length;
        uint256 depth;
        uint256 i;
        bool found;
        for (; i < n; ++i) {
            bytes1 ch = j[i];
            if (ch == '"') {
                if (depth == 1 && _at(j, i, pat)) {
                    i += pat.length;
                    found = true;
                    break;
                }
                i = _skipString(j, i);
            } else if (ch == "{" || ch == "[") {
                ++depth;
            } else if (ch == "}" || ch == "]") {
                --depth;
            }
        }
        require(found, "JsonSlice: key not found");
        while (j[i] != "[") ++i;
        ++i;
        uint256[] memory starts = new uint256[](4096);
        uint256[] memory ends = new uint256[](4096);
        uint256 m;
        depth = 0;
        for (; i < n; ++i) {
            bytes1 ch = j[i];
            if (ch == '"') {
                i = _skipString(j, i);
            } else if (ch == "{" || ch == "[") {
                if (depth == 0) starts[m] = i;
                ++depth;
            } else if (ch == "}" || ch == "]") {
                if (depth == 0) break; // the end of the array
                --depth;
                if (depth == 0) ends[m++] = i + 1;
            }
        }
        out = new string[](m);
        for (uint256 e; e < m; ++e) {
            bytes memory b = new bytes(ends[e] - starts[e]);
            for (uint256 k; k < b.length; ++k) {
                b[k] = j[starts[e] + k];
            }
            out[e] = string(b);
        }
    }

    /// @dev Returns the index of the closing quote of the string that opens at `i`.
    function _skipString(bytes memory j, uint256 i) private pure returns (uint256) {
        for (++i; j[i] != '"'; ++i) {
            if (j[i] == "\\") ++i;
        }
        return i;
    }

    function _at(bytes memory j, uint256 i, bytes memory pat) private pure returns (bool) {
        if (i + pat.length > j.length) return false;
        for (uint256 k; k < pat.length; ++k) {
            if (j[i + k] != pat[k]) return false;
        }
        return true;
    }
}
