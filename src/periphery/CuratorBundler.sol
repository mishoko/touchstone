// SPDX-License-Identifier: MIT
pragma solidity 0.8.26;

import {IPoolManager} from "@uniswap/v4-core/src/interfaces/IPoolManager.sol";
import {IUnlockCallback} from "@uniswap/v4-core/src/interfaces/callback/IUnlockCallback.sol";
import {IHooks} from "@uniswap/v4-core/src/interfaces/IHooks.sol";
import {PoolKey} from "@uniswap/v4-core/src/types/PoolKey.sol";
import {PoolId} from "@uniswap/v4-core/src/types/PoolId.sol";
import {Currency} from "@uniswap/v4-core/src/types/Currency.sol";
import {BalanceDelta} from "@uniswap/v4-core/src/types/BalanceDelta.sol";
import {SwapParams} from "@uniswap/v4-core/src/types/PoolOperation.sol";
import {StateLibrary} from "@uniswap/v4-core/src/libraries/StateLibrary.sol";
import {TickMath} from "@uniswap/v4-core/src/libraries/TickMath.sol";
import {IERC20} from "@openzeppelin/contracts/token/ERC20/IERC20.sol";
import {SafeERC20} from "@openzeppelin/contracts/token/ERC20/utils/SafeERC20.sol";
import {ITouchstoneFactory, HookArgs, SeedParams} from "../interfaces/ITouchstoneFactory.sol";
import {ITouchstoneHook} from "../interfaces/ITouchstoneHook.sol";
import {PreSwap} from "../types/PreSwap.sol";

/// @notice The vault's curator entry points (and its shares) as the bundler calls them.
interface ICuratorVault is IERC20 {
    function setPositions(int24 lo, int24 hi, PreSwap calldata preSwap, uint160 moveTo, uint256 maxCharge) external;
    function rebalanceSwap(bool zeroForOne, uint128 amountIn, uint256 maxCharge) external;
    function settle() external;
    function poke() external;
    function claim() external;
    function topUpBond(uint256 shares) external;
    function retire() external;
    function releaseBond() external;
}

/// @notice Everything `deployAndPlace` passes to the factory.
struct DeployArgs {
    bytes vaultCode;
    bytes hookCode;
    HookArgs hookArgs;
    bytes32 saltV;
    bytes32 saltH;
    SeedParams seed;
}

/// @title CuratorBundler
/// @notice The curator's contract: it deploys the vault through the factory (it is the salt sender and the seed payer)
///         and is the vault's curator. Every function is restricted to its owner. It places the first range in the
///         deployment transaction, re-aligns the pool's spot to the reference price before a placement (in its own
///         `unlock`, completed before the vault's call, so no unlock is nested), and passes the curator's calls through.
///         Shares it receives (claimed pay, a released bond) go to the owner. It holds the tokens the owner funds it
///         with for the seed and for spot moves; `sweep` returns them.
contract CuratorBundler is IUnlockCallback {
    using SafeERC20 for IERC20;
    using StateLibrary for IPoolManager;

    address public immutable OWNER;
    ITouchstoneFactory public immutable FACTORY;
    IPoolManager public immutable MANAGER;

    ICuratorVault public vault;
    ITouchstoneHook public hook;
    PoolKey internal _key;

    error OnlyOwner();
    error OnlyPoolManager();
    error Deployed();
    error Tolerance();

    constructor(address owner_, ITouchstoneFactory factory_) {
        OWNER = owner_;
        FACTORY = factory_;
        MANAGER = IPoolManager(factory_.POOL_MANAGER());
    }

    modifier onlyOwner() {
        // [MU-075] every function is the owner's
        if (msg.sender != OWNER) revert OnlyOwner();
        _;
    }

    // ------------------------------------------------------------------ deployment

    /// @notice Deploys the vault and its hook (this contract pays the seed and is the curator, so `seed.curator` must
    ///         be this contract), then places the first range with no move, in one transaction.
    function deployAndPlace(DeployArgs calldata a, int24 lo, int24 hi, PreSwap calldata preSwap, uint256 maxCharge)
        external
        onlyOwner
        returns (address v, address h)
    {
        // [MU-238] one deployment per bundler
        if (address(vault) != address(0)) revert Deployed();
        Currency c0 = a.hookArgs.currency0;
        Currency c1 = a.hookArgs.currency1;
        IERC20(Currency.unwrap(c0)).forceApprove(address(FACTORY), a.seed.a0 + a.seed.wit0);
        IERC20(Currency.unwrap(c1)).forceApprove(address(FACTORY), a.seed.a1 + a.seed.wit1);
        (v, h) = FACTORY.deploy(a.vaultCode, a.hookCode, a.hookArgs, a.saltV, a.saltH, a.seed);
        (vault, hook) = (ICuratorVault(v), ITouchstoneHook(h));
        _key = PoolKey(c0, c1, a.hookArgs.fee, a.hookArgs.tickSpacing, IHooks(h));
        ICuratorVault(v).setPositions(lo, hi, preSwap, 0, maxCharge);
    }

    // ------------------------------------------------------------------ spot

    /// @notice Moves the pool's spot to `sqrtTarget` with this contract's tokens (an exact-output swap stopped at the
    ///         target), in its own `unlock`.
    function moveSpot(uint160 sqrtTarget) external onlyOwner {
        _move(sqrtTarget);
    }

    /// @notice Re-aligns spot to `sqrtAlign` (the reference pool's price, read off-chain) and places, in one
    ///         transaction. Reverts `Tolerance` if spot is more than `tol` ticks from `sqrtAlign` before the
    ///         re-alignment (the curator's protection against a push ahead of its call).
    function moveAndPlace(
        uint160 sqrtAlign,
        uint24 tol,
        int24 lo,
        int24 hi,
        PreSwap calldata preSwap,
        uint160 moveTo,
        uint256 maxCharge
    ) external onlyOwner {
        (, int24 t,,) = MANAGER.getSlot0(_poolId());
        int256 dist = int256(t) - TickMath.getTickAtSqrtPrice(sqrtAlign);
        // [MU-237] the curator's spot tolerance, both sides
        if (dist > int256(uint256(tol)) || -dist > int256(uint256(tol))) revert Tolerance();
        _move(sqrtAlign);
        vault.setPositions(lo, hi, preSwap, moveTo, maxCharge);
    }

    function unlockCallback(bytes calldata data) external returns (bytes memory) {
        // [MU-236] the PoolManager calls back only the unlocker
        if (msg.sender != address(MANAGER)) revert OnlyPoolManager();
        uint160 target = abi.decode(data, (uint160));
        (uint160 sp,,,) = MANAGER.getSlot0(_poolId());
        // v4 rejects a price limit already reached
        if (sp == target) return "";
        bool z = target < sp;
        BalanceDelta d = MANAGER.swap(_key, SwapParams(z, type(int128).max, target), "");
        _settleDelta(_key.currency0, d.amount0());
        _settleDelta(_key.currency1, d.amount1());
        return "";
    }

    // ------------------------------------------------------------------ the curator's calls

    function setPositions(int24 lo, int24 hi, PreSwap calldata preSwap, uint160 moveTo, uint256 maxCharge)
        external
        onlyOwner
    {
        vault.setPositions(lo, hi, preSwap, moveTo, maxCharge);
    }

    function rebalanceSwap(bool zeroForOne, uint128 amountIn, uint256 maxCharge) external onlyOwner {
        vault.rebalanceSwap(zeroForOne, amountIn, maxCharge);
    }

    function settle() external onlyOwner {
        vault.settle();
    }

    function retire() external onlyOwner {
        vault.retire();
    }

    /// @notice Adds `shares` of the owner's to the bond (pulled from the owner).
    function topUpBond(uint256 shares) external onlyOwner {
        IERC20(address(vault)).safeTransferFrom(OWNER, address(this), shares);
        vault.topUpBond(shares);
    }

    /// @notice Releases the bond and forwards the shares to the owner.
    function releaseBond() external onlyOwner {
        vault.releaseBond();
        _forwardShares();
    }

    /// @notice Claims vested pay and forwards the shares to the owner.
    function claim() external onlyOwner {
        vault.claim();
        _forwardShares();
    }

    /// @notice Keep-alive: refreshes the hook's reference cache.
    function refreshRef() external onlyOwner {
        hook.refreshRef();
    }

    /// @notice Keep-alive: the vault's checkpoint and witness roll.
    function poke() external onlyOwner {
        vault.poke();
    }

    /// @notice Sends this contract's whole balance of `token` to `to`.
    function sweep(IERC20 token, address to) external onlyOwner {
        token.safeTransfer(to, token.balanceOf(address(this)));
    }

    // ------------------------------------------------------------------ internals

    function _move(uint160 target) internal {
        MANAGER.unlock(abi.encode(target));
    }

    /// @dev Pays a negative delta from this contract's tokens; takes a positive one.
    function _settleDelta(Currency c, int128 a) internal {
        if (a < 0) {
            MANAGER.sync(c);
            IERC20(Currency.unwrap(c)).safeTransfer(address(MANAGER), uint128(-a));
            MANAGER.settle();
        } else if (a > 0) {
            MANAGER.take(c, address(this), uint128(a));
        }
    }

    function _forwardShares() internal {
        uint256 b = vault.balanceOf(address(this));
        if (b > 0) IERC20(address(vault)).safeTransfer(OWNER, b);
    }

    function _poolId() internal view returns (PoolId) {
        return PoolId.wrap(keccak256(abi.encode(_key)));
    }
}
