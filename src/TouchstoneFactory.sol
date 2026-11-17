// SPDX-License-Identifier: MIT
pragma solidity 0.8.26;

import {Currency} from "@uniswap/v4-core/src/types/Currency.sol";
import {PoolKey} from "@uniswap/v4-core/src/types/PoolKey.sol";
import {IHooks} from "@uniswap/v4-core/src/interfaces/IHooks.sol";
import {IERC20} from "@openzeppelin/contracts/token/ERC20/IERC20.sol";
import {SafeERC20} from "@openzeppelin/contracts/token/ERC20/utils/SafeERC20.sol";
import {
    ITouchstoneFactory,
    ITouchstoneVaultSeed,
    HookArgs,
    SeedParams,
    Pending
} from "./interfaces/ITouchstoneFactory.sol";

/// @title TouchstoneFactory
/// @notice Deploys one vault and its hook in a single transaction and seeds the vault.
///         - The three creation codes (vault, extension, hook) are checked against hashes fixed at construction (the
///           vault's and the extension's take no constructor arguments; the hook's are checked without them).
///         - Salts are bound to the caller: the CREATE2 salt is `keccak256(abi.encode(msg.sender, salt))`, so a
///           front-runner reusing a salt lands on a different address.
///         - The vault and its extension (spec §4.12) have argument-free initcode; both constructors read `pending()`
///           (hook address, pool key, tier, and the two addresses, computed before either is created). The extension
///           is created first, under a salt derived from the vault's caller-bound salt, so the vault's constructor can
///           check it.
///         - The hook initcode is `hookCreationCode ++ abi.encode(hookArgs)`; its address carries the permission flag
///           bits, which the caller mines off-chain (see `script/Mine.s.sol`).
///         The factory has no owner and no privileged entry point.
contract TouchstoneFactory is ITouchstoneFactory {
    using SafeERC20 for IERC20;

    address public immutable POOL_MANAGER;
    bytes32 public immutable VAULT_CODE_HASH;
    bytes32 public immutable EXTENSION_CODE_HASH;
    bytes32 public immutable HOOK_CODE_HASH;

    Pending internal _pending;

    constructor(address poolManager, bytes32 vaultCodeHash, bytes32 extensionCodeHash, bytes32 hookCodeHash) {
        POOL_MANAGER = poolManager;
        VAULT_CODE_HASH = vaultCodeHash;
        EXTENSION_CODE_HASH = extensionCodeHash;
        HOOK_CODE_HASH = hookCodeHash;
    }

    function pending() external view returns (Pending memory) {
        return _pending;
    }

    function deploy(
        bytes calldata vaultCreationCode,
        bytes calldata extensionCreationCode,
        bytes calldata hookCreationCode,
        HookArgs calldata hookArgs,
        bytes32 saltV,
        bytes32 saltH,
        SeedParams calldata seedParams
    ) external returns (address vault, address hook) {
        // [MU-269] the extension's code is checked like the vault's and the hook's
        if (
            keccak256(vaultCreationCode) != VAULT_CODE_HASH || keccak256(extensionCreationCode) != EXTENSION_CODE_HASH
                || keccak256(hookCreationCode) != HOOK_CODE_HASH
        ) revert CodeHash();
        Currency c0 = hookArgs.currency0;
        Currency c1 = hookArgs.currency1;
        // ERC-20 only: the zero currency (native ETH) is rejected.
        if (c0.isAddressZero() || c1.isAddressZero()) revert BadToken();

        // [MU-135] salts are bound to the caller
        bytes32 sv = keccak256(abi.encode(msg.sender, saltV));
        bytes32 sh = keccak256(abi.encode(msg.sender, saltH));
        bytes memory hookInit = abi.encodePacked(hookCreationCode, abi.encode(hookArgs));
        address vaultAddr = _create2Address(sv, VAULT_CODE_HASH);
        bytes32 se = keccak256(abi.encode(sv, "ext"));
        address extAddr = _create2Address(se, EXTENSION_CODE_HASH);
        address hookAddr = _create2Address(sh, keccak256(hookInit));
        // The hook's immutables must point at this deployment's PoolManager and vault.
        if (hookArgs.poolManager != POOL_MANAGER || hookArgs.vault != vaultAddr) revert WrongArgs();

        _pending.hook = hookAddr;
        _pending.key = PoolKey(c0, c1, hookArgs.fee, hookArgs.tickSpacing, IHooks(hookAddr));
        _pending.tier = hookArgs.tier;
        _pending.vault = vaultAddr;
        _pending.extension = extAddr;

        // Both constructors read `pending()`; the extension first, since the vault's constructor checks it.
        address extension = _create2(extensionCreationCode, se);
        vault = _create2(vaultCreationCode, sv);
        if (ITouchstoneVaultSeed(vault).TIER_HASH() != keccak256(abi.encode(hookArgs.tier))) {
            revert TierMismatch();
        }
        delete _pending;

        // The hook constructor checks its address flags and initializes the pool at the seed price.
        hook = _create2(hookInit, sh);

        _pull(c0, seedParams.a0 + seedParams.wit0, vault);
        _pull(c1, seedParams.a1 + seedParams.wit1, vault);
        // The vault's `seed` settles the transferred amounts into the PoolManager as claims.
        ITouchstoneVaultSeed(vault)
            .seed(seedParams.a0, seedParams.a1, seedParams.curator, seedParams.wit0, seedParams.wit1);

        emit Deployed(msg.sender, vault, extension, hook, hookArgs.tier);
    }

    /// @dev Pulls `amount` from the caller to the vault and checks the vault's balance moved by exactly `amount`.
    function _pull(Currency c, uint256 amount, address to) internal {
        IERC20 token = IERC20(Currency.unwrap(c));
        uint256 before = token.balanceOf(to);
        token.safeTransferFrom(msg.sender, to, amount);
        if (token.balanceOf(to) - before != amount) revert SeedTransfer();
    }

    /// @dev CREATE2; a failing constructor's revert data is bubbled up.
    function _create2(bytes memory initCode, bytes32 salt) internal returns (address a) {
        assembly ("memory-safe") {
            a := create2(0, add(initCode, 0x20), mload(initCode), salt)
            if iszero(a) {
                let p := mload(0x40)
                returndatacopy(p, 0, returndatasize())
                revert(p, returndatasize())
            }
        }
    }

    function _create2Address(bytes32 salt, bytes32 initCodeHash) internal view returns (address) {
        return address(uint160(uint256(keccak256(abi.encodePacked(bytes1(0xff), address(this), salt, initCodeHash)))));
    }
}
