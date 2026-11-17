// SPDX-License-Identifier: MIT
pragma solidity 0.8.26;

import {Currency} from "@uniswap/v4-core/src/types/Currency.sol";
import {PoolKey} from "@uniswap/v4-core/src/types/PoolKey.sol";
import {Tier} from "../types/Tier.sol";

/// @notice The hook's constructor arguments. The hook initcode is `hookCreationCode ++ abi.encode(HookArgs)`.
struct HookArgs {
    address poolManager;
    address vault;
    Currency currency0;
    Currency currency1;
    uint24 fee;
    int24 tickSpacing;
    uint160 seedSqrtPriceX96;
    Tier tier;
}

/// @notice Seed amounts; the payer is the `deploy` caller.
struct SeedParams {
    uint256 a0;
    uint256 a1;
    address curator;
    uint256 wit0;
    uint256 wit1;
}

/// @notice What the vault constructor reads back from the factory during `deploy`.
struct Pending {
    address hook;
    PoolKey key;
    Tier tier;
    /// @dev The vault's and its extension's addresses, computed before either is created (spec §4.12).
    address vault;
    address extension;
}

/// @notice The part of the vault the factory calls.
interface ITouchstoneVaultSeed {
    function TIER_HASH() external view returns (bytes32);
    function seed(uint256 a0, uint256 a1, address curator, uint256 wit0, uint256 wit1) external;
}

interface ITouchstoneFactory {
    error CodeHash();
    error WrongArgs();
    error TierMismatch();
    error BadToken();
    error SeedTransfer();

    /// @notice The tier is emitted because the vault keeps only its hash (`TIER_HASH`): readers take it from here.
    event Deployed(address indexed sender, address vault, address extension, address hook, Tier tier);

    function POOL_MANAGER() external view returns (address);
    function VAULT_CODE_HASH() external view returns (bytes32);
    function EXTENSION_CODE_HASH() external view returns (bytes32);
    function HOOK_CODE_HASH() external view returns (bytes32);
    function pending() external view returns (Pending memory);
    function deploy(
        bytes calldata vaultCreationCode,
        bytes calldata extensionCreationCode,
        bytes calldata hookCreationCode,
        HookArgs calldata hookArgs,
        bytes32 saltV,
        bytes32 saltH,
        SeedParams calldata seedParams
    ) external returns (address vault, address hook);
}
