// SPDX-License-Identifier: MIT
pragma solidity 0.8.26;

import {Test} from "forge-std/Test.sol";
import {PoolManager} from "@uniswap/v4-core/src/PoolManager.sol";
import {IPoolManager} from "@uniswap/v4-core/src/interfaces/IPoolManager.sol";
import {IHooks} from "@uniswap/v4-core/src/interfaces/IHooks.sol";
import {Hooks} from "@uniswap/v4-core/src/libraries/Hooks.sol";
import {TickMath} from "@uniswap/v4-core/src/libraries/TickMath.sol";
import {Currency} from "@uniswap/v4-core/src/types/Currency.sol";
import {PoolKey} from "@uniswap/v4-core/src/types/PoolKey.sol";
import {HookArgs} from "../../src/interfaces/ITouchstoneFactory.sol";
import {Obs} from "../../src/types/Obs.sol";
import {Tier} from "../../src/types/Tier.sol";
import {Tiers} from "../../script/Tiers.sol";
import {HookHarness} from "./HookHarness.sol";
import {MockRefPool} from "./MockRefPool.sol";
import {MockToken} from "./Stubs.sol";

/// @notice Unit-test base for the hook: a PoolManager, two tokens, a scripted reference pool and a HookHarness with a
///         scripted pool price, at the TEST tier (or DEMO). The vault is a fixed address that the tests prank.
///         `advance(n)` rolls n blocks and warps n * blocktime seconds.
abstract contract HookBase is Test {
    uint160 internal constant FLAGS = Hooks.BEFORE_INITIALIZE_FLAG | Hooks.BEFORE_SWAP_FLAG | Hooks.BEFORE_DONATE_FLAG;
    address internal constant VAULT = address(0x5A17);
    int24 internal constant LO = -1000;
    int24 internal constant HI = 1000;

    PoolManager internal manager;
    MockToken internal token0;
    MockToken internal token1;
    MockRefPool internal ref;
    HookHarness internal h;
    Tier internal tier;
    uint256 internal blocktime;
    uint256 internal deployCount;

    function _setUpHook(bool demo) internal {
        (Tier memory t, uint256 bt) = demo ? Tiers.demo() : Tiers.testing();
        blocktime = bt;
        // the timestamp moves with blocks: ts = block * blocktime + phase
        vm.roll(10_000);
        vm.warp(10_000 * bt);
        manager = new PoolManager(address(this));
        MockToken a = new MockToken("A", 18);
        MockToken b = new MockToken("B", 18);
        (token0, token1) = address(a) < address(b) ? (a, b) : (b, a);
        ref = new MockRefPool(address(token0), address(token1), 500, 0, 0);
        t.REF_POOL = address(ref);
        t.REF_SQRT_PRICE = TickMath.getSqrtPriceAtTick(0);
        tier = t;
        h = _deployHarness(t);
        pool(0);
    }

    function _args(Tier memory t, address vault) internal view returns (HookArgs memory) {
        return HookArgs({
            poolManager: address(manager),
            vault: vault,
            currency0: Currency.wrap(address(token0)),
            currency1: Currency.wrap(address(token1)),
            fee: 500,
            tickSpacing: 10,
            seedSqrtPriceX96: TickMath.getSqrtPriceAtTick(0),
            tier: t
        });
    }

    function _flagged() internal returns (address) {
        return address(uint160(++deployCount << 100) | FLAGS);
    }

    function _deployHarness(Tier memory t) internal returns (HookHarness hh) {
        address at = _flagged();
        deployCodeTo("HookHarness.sol:HookHarness", abi.encode(_args(t, VAULT)), at);
        hh = HookHarness(at);
    }

    // ------------------------------------------------------------------ scripting

    /// @notice The reference TWAP is `t` for every window.
    function refConst(int24 t) internal {
        ref.setMode(MockRefPool.Mode.Ok);
        uint32[] memory s = new uint32[](1);
        int24[] memory k = new int24[](1);
        k[0] = t;
        ref.setPath(s, k);
    }

    function refMode(MockRefPool.Mode m) internal {
        ref.setMode(m);
    }

    /// @notice The pool price is exactly at `tick`.
    function pool(int24 tick) internal {
        h.setSlot0(TickMath.getSqrtPriceAtTick(tick), tick);
    }

    function poolSqrt(uint160 sqrtP) internal {
        h.setSlot0(sqrtP, TickMath.getTickAtSqrtPrice(sqrtP));
    }

    function advance(uint256 n) internal {
        vm.roll(vm.getBlockNumber() + n);
        vm.warp(vm.getBlockTimestamp() + n * blocktime);
    }

    // ------------------------------------------------------------------ calls

    function snap() internal returns (Obs memory) {
        return h.snapshotIfNewBlock();
    }

    function touch() internal returns (Obs memory) {
        h.refreshRef();
        return h.obs();
    }

    function arm() internal {
        snap();
        vm.prank(VAULT);
        h.armAtSeed();
    }

    function register(int24 lo, int24 hi, int24 live) internal {
        snap();
        vm.prank(VAULT);
        h.setVaultRange(lo, hi, live);
    }

    function ack(uint32 n) internal {
        vm.prank(VAULT);
        h.ackBreaches(n);
    }

    function grid() internal view returns (uint256) {
        return tier.GRID;
    }
}
