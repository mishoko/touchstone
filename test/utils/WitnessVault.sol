// SPDX-License-Identifier: MIT
pragma solidity 0.8.26;

import {IPoolManager} from "@uniswap/v4-core/src/interfaces/IPoolManager.sol";
import {IUnlockCallback} from "@uniswap/v4-core/src/interfaces/callback/IUnlockCallback.sol";
import {Currency} from "@uniswap/v4-core/src/types/Currency.sol";
import {IERC20} from "@openzeppelin/contracts/token/ERC20/IERC20.sol";
import {ITouchstoneHook} from "../../src/interfaces/ITouchstoneHook.sol";

/// @notice A vault stand-in for hook tests on a live pool: it funds the witness with ERC-6909 claims and rolls it
///         inside its own `unlock`, as the vault does at seed and in `poke`.
contract WitnessVault is IUnlockCallback {
    IPoolManager public immutable manager;
    Currency public immutable c0;
    Currency public immutable c1;
    ITouchstoneHook public hook;

    constructor(IPoolManager m, Currency a, Currency b) {
        (manager, c0, c1) = (m, a, b);
    }

    function setHook(address h) external {
        hook = ITouchstoneHook(h);
    }

    /// @notice Tokens must already be held by this contract.
    function fund(uint256 a0, uint256 a1) external {
        manager.unlock(abi.encode(uint8(0), abi.encode(a0, a1)));
    }

    function roll(uint32 epoch) external returns (uint256 fg0, uint256 fg1, uint128 lw, bool fb) {
        return abi.decode(manager.unlock(abi.encode(uint8(1), abi.encode(epoch))), (uint256, uint256, uint128, bool));
    }

    function unlockCallback(bytes calldata data) external returns (bytes memory) {
        require(msg.sender == address(manager));
        (uint8 op, bytes memory args) = abi.decode(data, (uint8, bytes));
        if (op == 0) {
            (uint256 a0, uint256 a1) = abi.decode(args, (uint256, uint256));
            _claim(c0, a0);
            _claim(c1, a1);
            hook.fundWitness(a0, a1);
            return "";
        }
        uint32 epoch = abi.decode(args, (uint32));
        (uint256 fg0, uint256 fg1, uint128 lw, bool fb,) = hook.rollWitness(epoch);
        return abi.encode(fg0, fg1, lw, fb);
    }

    function _claim(Currency c, uint256 a) internal {
        manager.sync(c);
        IERC20(Currency.unwrap(c)).transfer(address(manager), a);
        manager.settle();
        manager.mint(address(hook), c.toId(), a);
    }
}
