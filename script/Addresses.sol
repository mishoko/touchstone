// SPDX-License-Identifier: MIT
pragma solidity 0.8.26;

/// @notice Canonical external contracts per chain. Every address is copied from the official Uniswap deployments
///         pages named next to it (mixed-case copies kept verbatim; all-lowercase copies converted to their EIP-55
///         checksum with `cast to-check-sum-address`). `test/fork/Addresses.t.sol` checks that each has code.
library Addresses {
    // ---------------------------------------------------------------- Ethereum mainnet (chain 1)
    // Source: https://developers.uniswap.org/docs/protocols/v4/deployments
    address internal constant MAINNET_POOL_MANAGER = 0x000000000004444c5dc75cB358380D2e3dE08A90;
    // Source: https://developers.uniswap.org/docs/protocols/v4/deployments ("Universal Router"; page lowercase)
    address internal constant MAINNET_UNIVERSAL_ROUTER = 0x66a9893cC07D91D95644AEDD05D03f95e1dBA8Af;
    // Source: https://developers.uniswap.org/docs/protocols/v4/deployments
    address internal constant MAINNET_PERMIT2 = 0x000000000022D473030F116dDEE9F6B43aC78BA3;
    // Source: https://developers.uniswap.org/docs/protocols/v4/deployments ("Quoter", V4Quoter.sol)
    address internal constant MAINNET_V4_QUOTER = 0x99bd42B2B22e531f283b7F8a86dFf631EB16A9D1;
    // Source: https://developers.uniswap.org/docs/protocols/v3/deployments/v3-ethereum-deployments
    address internal constant MAINNET_V3_FACTORY = 0x1F98431c8aD98523631AE4a59f267346ea31F984;
    // The USDC/WETH 0.05% v3 pool (the tier A price reference). Not on the deployments pages: the fork test checks
    // that the mainnet v3 factory's `getPool(token0, token1, 500)` returns this address and that its tokens are
    // USDC and WETH.
    address internal constant MAINNET_REF_POOL_USDC_WETH_500 = 0x88e6A0c2dDD26FEEb64F039a2c41296FcB3f5640;

    // ---------------------------------------------------------------- Unichain Sepolia (chain 1301)
    // Source: https://developers.uniswap.org/docs/protocols/v4/deployments (page lowercase)
    address internal constant UNICHAIN_SEPOLIA_POOL_MANAGER = 0x00B036B58a818B1BC34d502D3fE730Db729e62AC;
    // Source: https://developers.uniswap.org/docs/protocols/v4/deployments ("Universal Router"; page lowercase)
    address internal constant UNICHAIN_SEPOLIA_UNIVERSAL_ROUTER = 0xf70536B3bcC1bD1a972dc186A2cf84cC6da6Be5D;
    // Source: https://developers.uniswap.org/docs/protocols/v4/deployments
    address internal constant UNICHAIN_SEPOLIA_PERMIT2 = 0x000000000022D473030F116dDEE9F6B43aC78BA3;
    // Source: https://developers.uniswap.org/docs/protocols/v4/deployments ("Quoter", V4Quoter.sol)
    address internal constant UNICHAIN_SEPOLIA_V4_QUOTER = 0x572B9cDBcd8c2b8F3Fe849B9d250F35284455d7f;
    // Source: https://developers.uniswap.org/docs/protocols/v3/deployments/v3-unichain-deployments (Unichain Sepolia)
    address internal constant UNICHAIN_SEPOLIA_V3_FACTORY = 0x1F98431c8aD98523631AE4a59f267346ea31F984;

    struct Chain {
        address poolManager;
        address universalRouter;
        address permit2;
        address v4Quoter;
        address v3Factory;
    }

    function forChain(uint256 chainId) internal pure returns (Chain memory c) {
        if (chainId == 1) {
            c = Chain(MAINNET_POOL_MANAGER, MAINNET_UNIVERSAL_ROUTER, MAINNET_PERMIT2, MAINNET_V4_QUOTER, MAINNET_V3_FACTORY);
        } else if (chainId == 1301) {
            c = Chain(
                UNICHAIN_SEPOLIA_POOL_MANAGER,
                UNICHAIN_SEPOLIA_UNIVERSAL_ROUTER,
                UNICHAIN_SEPOLIA_PERMIT2,
                UNICHAIN_SEPOLIA_V4_QUOTER,
                UNICHAIN_SEPOLIA_V3_FACTORY
            );
        } else {
            revert("Addresses: unsupported chain");
        }
    }
}
