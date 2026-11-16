// SPDX-License-Identifier: MIT
pragma solidity 0.8.26;

import {IPoolManager} from "@uniswap/v4-core/src/interfaces/IPoolManager.sol";
import {IUnlockCallback} from "@uniswap/v4-core/src/interfaces/callback/IUnlockCallback.sol";
import {Currency} from "@uniswap/v4-core/src/types/Currency.sol";
import {DeployBaseUnplaced} from "./DeployBaseUnplaced.sol";
import {MockToken} from "./Stubs.sol";
import {TouchstoneVault} from "../../src/TouchstoneVault.sol";

/// @notice Test base with the real vault deployed through the factory (EMPTY range, TEST tier).
abstract contract VaultBase is DeployBaseUnplaced {
    TouchstoneVault internal v;
    address internal alice = makeAddr("alice");
    address internal bob = makeAddr("bob");

    function _vaultCode() internal pure virtual override returns (bytes memory) {
        return type(TouchstoneVault).creationCode;
    }

    function setUp() public virtual override {
        super.setUp();
        v = TouchstoneVault(vault);
    }

    /// @notice Funds `who` with `n` of each token and approves the vault.
    function _fundUser(address who, uint256 n) internal {
        token0.mint(who, n);
        token1.mint(who, n);
        vm.startPrank(who);
        token0.approve(vault, type(uint256).max);
        token1.approve(vault, type(uint256).max);
        vm.stopPrank();
    }

    function _deposit(address who, uint256 s) internal returns (uint256 a0, uint256 a1) {
        vm.prank(who);
        return v.deposit(s, type(uint256).max, type(uint256).max, who);
    }

    function _withdraw(address who, uint256 s) internal returns (uint256 a0, uint256 a1) {
        vm.prank(who);
        return v.withdraw(s, 0, 0, who);
    }
}

/// @notice Mints ERC-6909 claims to an arbitrary owner (an unsolicited claim transfer to the vault).
contract ClaimDonor is IUnlockCallback {
    IPoolManager internal immutable manager;

    constructor(IPoolManager m) {
        manager = m;
    }

    function donate(MockToken token, address to, uint256 amount) external {
        manager.unlock(abi.encode(token, to, amount));
    }

    function unlockCallback(bytes calldata data) external returns (bytes memory) {
        (MockToken token, address to, uint256 amount) = abi.decode(data, (MockToken, address, uint256));
        Currency c = Currency.wrap(address(token));
        manager.sync(c);
        token.transfer(address(manager), amount);
        manager.settle();
        manager.mint(to, c.toId(), amount);
        return "";
    }
}
