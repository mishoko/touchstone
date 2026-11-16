// SPDX-License-Identifier: MIT
pragma solidity 0.8.26;

import {Test} from "forge-std/Test.sol";
import {Addresses} from "../../script/Addresses.sol";

interface IV3Factory {
    function getPool(address a, address b, uint24 fee) external view returns (address);
}

interface IV3PoolTokens {
    function token0() external view returns (address);
    function token1() external view returns (address);
    function fee() external view returns (uint24);
}

interface ISymbol {
    function symbol() external view returns (string memory);
}

/// @notice Every canonical address has code on its chain. Runs with
///         `FOUNDRY_PROFILE=fork forge test --mp test/fork`; each chain's test skips when its RPC variable is unset.
contract AddressesForkTest is Test {
    function _fork(string memory envVar, uint256 chainId) internal returns (bool) {
        string memory url = vm.envOr(envVar, string(""));
        if (bytes(url).length == 0) {
            vm.skip(true);
            return false;
        }
        vm.createSelectFork(url);
        assertEq(block.chainid, chainId, "rpc chain id");
        return true;
    }

    function _hasCode(address a, string memory name) internal view {
        bytes32 h;
        assembly {
            h := extcodehash(a)
        }
        assertTrue(h != bytes32(0) && h != keccak256(""), name);
    }

    function _checkChain(uint256 chainId) internal view {
        Addresses.Chain memory c = Addresses.forChain(chainId);
        _hasCode(c.poolManager, "PoolManager");
        _hasCode(c.universalRouter, "UniversalRouter");
        _hasCode(c.permit2, "Permit2");
        _hasCode(c.v4Quoter, "V4Quoter");
        _hasCode(c.v3Factory, "V3Factory");
    }

    function test_addresses_mainnetHaveCode() public {
        if (!_fork("MAINNET_RPC_URL", 1)) return;
        _checkChain(1);
        // The tier A reference pool is the canonical v3 USDC/WETH 0.05% pool.
        IV3PoolTokens p = IV3PoolTokens(Addresses.MAINNET_REF_POOL_USDC_WETH_500);
        _hasCode(address(p), "reference pool");
        assertEq(p.fee(), 500);
        assertEq(IV3Factory(Addresses.MAINNET_V3_FACTORY).getPool(p.token0(), p.token1(), 500), address(p));
        assertEq(ISymbol(p.token0()).symbol(), "USDC");
        assertEq(ISymbol(p.token1()).symbol(), "WETH");
    }

    function test_addresses_unichainSepoliaHaveCode() public {
        if (!_fork("UNICHAIN_SEPOLIA_RPC_URL", 1301)) return;
        _checkChain(1301);
    }
}
