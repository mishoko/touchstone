// SPDX-License-Identifier: MIT
pragma solidity 0.8.26;

import {Test} from "forge-std/Test.sol";

/// @notice The one way a fork test selects a fork (F-052): the RPC is read from `envVar`, the test skips when it is
///         unset, the fork is pinned to `FORK_BLOCK_<chainId>` when set (per chain, so
///         a block number of one chain never pins another), and the chain id is asserted, so a variable that points
///         at another chain fails instead of measuring the wrong chain.
abstract contract ForkBase is Test {
    function _fork(string memory envVar, uint256 chainId) internal returns (bool) {
        string memory url = vm.envOr(envVar, string(""));
        if (bytes(url).length == 0) {
            vm.skip(true);
            return false;
        }
        // FORK_BLOCK_<chainId> pins the fork so that Foundry's RPC cache serves repeated runs (public RPCs rate-limit)
        uint256 fb = vm.envOr(string.concat("FORK_BLOCK_", vm.toString(chainId)), uint256(0));
        if (fb == 0) vm.createSelectFork(url);
        else vm.createSelectFork(url, fb);
        assertEq(block.chainid, chainId, "rpc chain id");
        return true;
    }
}
