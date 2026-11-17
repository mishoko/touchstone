// SPDX-License-Identifier: MIT
pragma solidity 0.8.26;

import {IPoolManager} from "@uniswap/v4-core/src/interfaces/IPoolManager.sol";
import {IHooks} from "@uniswap/v4-core/src/interfaces/IHooks.sol";
import {Hooks} from "@uniswap/v4-core/src/libraries/Hooks.sol";
import {PoolKey} from "@uniswap/v4-core/src/types/PoolKey.sol";
import {ERC20} from "@openzeppelin/contracts/token/ERC20/ERC20.sol";
import {Tier} from "../../src/types/Tier.sol";
import {ITouchstoneFactory, HookArgs, Pending} from "../../src/interfaces/ITouchstoneFactory.sol";

/// @notice Stand-in vault for factory tests: an argument-free constructor that reads `factory.pending()`.
contract StubVault {
    address public immutable FACTORY;
    address public immutable HOOK;
    PoolKey internal _key;
    Tier internal _tier;
    uint256[4] public seeded; // a0, a1, wit0, wit1
    address public curator;
    uint256 public seedCalls;

    constructor() {
        FACTORY = msg.sender;
        Pending memory p = ITouchstoneFactory(msg.sender).pending();
        HOOK = p.hook;
        _key = p.key;
        _tier = p.tier;
    }

    function tier() external view returns (Tier memory) {
        return _tier;
    }

    function TIER_HASH() external view virtual returns (bytes32) {
        return keccak256(abi.encode(_tier));
    }

    function key() external view returns (PoolKey memory) {
        return _key;
    }

    function seed(uint256 a0, uint256 a1, address curator_, uint256 wit0, uint256 wit1) external {
        require(msg.sender == FACTORY, "OnlyFactory");
        seeded = [a0, a1, wit0, wit1];
        curator = curator_;
        seedCalls++;
    }
}

/// @notice The extension's stand-in for the factory tests (the stub vault does not use it).
contract StubExtension {}

/// @notice A vault whose reported tier differs from the one it was handed.
contract StubVaultBadTier is StubVault {
    function TIER_HASH() external view override returns (bytes32) {
        Tier memory t = _tier;
        t.CURE_BLOCKS += 1;
        return keccak256(abi.encode(t));
    }
}

/// @notice Stand-in hook: checks its address flags and initializes the pool from its constructor (the PoolManager
///         skips a hook's callbacks on the hook's own call).
contract StubHook {
    address public immutable POOL_MANAGER;
    address public immutable VAULT;
    Tier internal _tier;

    constructor(HookArgs memory a) {
        Hooks.validateHookPermissions(
            IHooks(address(this)),
            Hooks.Permissions({
                beforeInitialize: true,
                afterInitialize: false,
                beforeAddLiquidity: false,
                afterAddLiquidity: false,
                beforeRemoveLiquidity: false,
                afterRemoveLiquidity: false,
                beforeSwap: true,
                afterSwap: false,
                beforeDonate: true,
                afterDonate: false,
                beforeSwapReturnDelta: false,
                afterSwapReturnDelta: false,
                afterAddLiquidityReturnDelta: false,
                afterRemoveLiquidityReturnDelta: false
            })
        );
        POOL_MANAGER = a.poolManager;
        VAULT = a.vault;
        _tier = a.tier;
        IPoolManager(a.poolManager)
            .initialize(PoolKey(a.currency0, a.currency1, a.fee, a.tickSpacing, IHooks(address(this))), a.seedSqrtPriceX96);
    }

    function tier() external view returns (Tier memory) {
        return _tier;
    }
}

contract MockToken is ERC20 {
    uint8 internal immutable _dec;

    constructor(string memory n, uint8 d) ERC20(n, n) {
        _dec = d;
    }

    function decimals() public view override returns (uint8) {
        return _dec;
    }

    function mint(address to, uint256 amount) external {
        _mint(to, amount);
    }
}
