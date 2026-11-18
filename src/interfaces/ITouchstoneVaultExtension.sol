// SPDX-License-Identifier: MIT
pragma solidity 0.8.26;

import {Entry} from "../types/Entry.sol";
import {Obs} from "../types/Obs.sol";

/// @notice The vault's entry points that live in its extension. They are called on the VAULT's address: its fallback
///         forwards exactly these selectors (the allow-list) to the extension by `delegatecall`.
interface IVaultEntries {
    function seed(uint256 a0, uint256 a1, address curator, uint256 wit0, uint256 wit1) external;
    function deposit(uint256 shares, uint256 max0, uint256 max1, address to) external returns (uint256 a0, uint256 a1);
    function withdraw(uint256 shares, uint256 min0, uint256 min1, address to) external returns (uint256 a0, uint256 a1);
    function claim() external;
    function topUpBond(uint256 shares) external;
    function retire() external;
    function releaseBond() external;
    function poke() external;
    function processBreach() external;
}

/// @notice The per-vault extension (spec §4.12, lever (c)): the cold paths of one vault, run only by `delegatecall`
///         from that vault, on the vault's storage.
interface ITouchstoneVaultExtension is IVaultEntries {
    /// @notice The moved flows' bodies; called by the vault's `unlockCallback` (by `delegatecall`) only.
    function dispatch(Entry entry, address payer, bytes calldata args) external returns (bytes memory);
    /// @notice The epoch roll's record write (spec §4.6 step (6)); called by the vault's checkpoint (by `delegatecall`) only.
    function roll(Obs calldata o, uint32 ne) external;
    function VAULT() external view returns (address);
    function SELF() external view returns (address);
    function CONFIG_HASH() external view returns (bytes32);
    function TIER_HASH() external view returns (bytes32);
}
