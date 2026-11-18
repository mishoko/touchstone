// SPDX-License-Identifier: MIT
pragma solidity 0.8.26;

import {ERC20} from "@openzeppelin/contracts/token/ERC20/ERC20.sol";
import {IERC20} from "@openzeppelin/contracts/token/ERC20/IERC20.sol";
import {SafeERC20} from "@openzeppelin/contracts/token/ERC20/utils/SafeERC20.sol";

/// @notice Testnet-only helpers for the DEMO tier (spec §4.8 step 0b). Nothing here ships in `src/`.

/// @notice A mock token whose minting is restricted to its deployer, so that no third party can fund trades that move
///         the testnet reference pool (disclosed: on testnet the reference is ours).
contract DemoToken is ERC20 {
    address public immutable minter;
    uint8 internal immutable _dec;

    error OnlyMinter();

    constructor(string memory name_, string memory symbol_, uint8 d) ERC20(name_, symbol_) {
        minter = msg.sender;
        _dec = d;
    }

    function decimals() public view override returns (uint8) {
        return _dec;
    }

    function mint(address to, uint256 amount) external {
        if (msg.sender != minter) revert OnlyMinter();
        _mint(to, amount);
    }
}

/// @notice The subset of the Uniswap v3 factory and pool used to create and drive the mock-token reference pool.
interface IV3Factory {
    function createPool(address a, address b, uint24 fee) external returns (address);
    function getPool(address a, address b, uint24 fee) external view returns (address);
}

interface IV3Pool {
    function initialize(uint160 sqrtPriceX96) external;
    function increaseObservationCardinalityNext(uint16 next) external;
    function mint(address recipient, int24 lo, int24 hi, uint128 amount, bytes calldata data)
        external
        returns (uint256, uint256);
    function swap(address recipient, bool zeroForOne, int256 amount, uint160 limit, bytes calldata data)
        external
        returns (int256, int256);
    function slot0() external view returns (uint160, int24, uint16, uint16, uint16, uint8, bool);
    function token0() external view returns (address);
    function token1() external view returns (address);
}

/// @notice Owner-only driver of the reference pool: adds liquidity and trades from its own token balance. The v3
///         callbacks pay only the one pool this driver was bound to.
contract RefDriver {
    using SafeERC20 for IERC20;

    address public immutable owner;
    IV3Pool public immutable pool;
    IERC20 internal immutable T0;
    IERC20 internal immutable T1;
    uint160 internal constant MIN_SQRT = 4295128740;
    uint160 internal constant MAX_SQRT = 1461446703485210103287273052203988822378723970341;

    error OnlyOwner();
    error OnlyPool();

    constructor(address owner_, IV3Pool pool_) {
        owner = owner_;
        pool = pool_;
        T0 = IERC20(pool_.token0());
        T1 = IERC20(pool_.token1());
    }

    modifier onlyOwner() {
        if (msg.sender != owner) revert OnlyOwner();
        _;
    }

    function addLiquidity(int24 lo, int24 hi, uint128 l) external onlyOwner returns (uint256, uint256) {
        return pool.mint(address(this), lo, hi, l, "");
    }

    /// @notice An exact-input swap of `amountIn`, stopped at `limit` (0 = no limit).
    function swap(bool zeroForOne, uint256 amountIn, uint160 limit) external onlyOwner {
        if (limit == 0) limit = zeroForOne ? MIN_SQRT : MAX_SQRT;
        pool.swap(address(this), zeroForOne, int256(amountIn), limit, "");
    }

    function uniswapV3MintCallback(uint256 owed0, uint256 owed1, bytes calldata) external {
        if (msg.sender != address(pool)) revert OnlyPool();
        if (owed0 > 0) T0.safeTransfer(msg.sender, owed0);
        if (owed1 > 0) T1.safeTransfer(msg.sender, owed1);
    }

    function uniswapV3SwapCallback(int256 d0, int256 d1, bytes calldata) external {
        if (msg.sender != address(pool)) revert OnlyPool();
        if (d0 > 0) T0.safeTransfer(msg.sender, uint256(d0));
        if (d1 > 0) T1.safeTransfer(msg.sender, uint256(d1));
    }
}
