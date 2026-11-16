// SPDX-License-Identifier: MIT
pragma solidity 0.8.26;

/// @notice The snapshot ring (records only): 32 entries of `uint64(block << 24 | uint24(tick))`, and the median of
///         the most recent nine.
library MedianOracle {
    uint256 internal constant N_SNAP = 32;
    uint256 internal constant K_MED = 9;

    function pack(uint256 blockNumber, int24 tick) internal pure returns (uint64) {
        return uint64((blockNumber << 24) | uint256(uint24(tick)));
    }

    function tickOf(uint64 entry) internal pure returns (int24) {
        return int24(uint24(entry));
    }

    /// @notice The median of the last min(count, 9) entries; the lower median for an even count; 0 when empty.
    ///         Insertion sort into a fixed 9-slot memory array as the entries are read (newest first).
    function med9(uint64[32] storage ring, uint256 head, uint256 count) internal view returns (int24) {
        unchecked {
            uint256 n = count < K_MED ? count : K_MED;
            if (n == 0) return 0;
            int24[9] memory a;
            uint256 newest = head + N_SNAP - 1;
            for (uint256 i; i < n; ++i) {
                int24 x = tickOf(ring[(newest - i) % N_SNAP]);
                uint256 j = i;
                while (j != 0 && a[j - 1] > x) {
                    a[j] = a[j - 1];
                    --j;
                }
                a[j] = x;
            }
            // [MU-145] the lower median of the newest min(count, 9) entries
            return a[(n - 1) / 2];
        }
    }

    /// @notice Insertion sort, then the element at index (n - 1) / 2. `a` is sorted in place.
    function median(int24[] memory a) internal pure returns (int24) {
        uint256 n = a.length;
        for (uint256 i = 1; i < n; ++i) {
            int24 x = a[i];
            uint256 j = i;
            while (j > 0 && a[j - 1] > x) {
                a[j] = a[j - 1];
                --j;
            }
            a[j] = x;
        }
        return a[(n - 1) / 2];
    }
}
