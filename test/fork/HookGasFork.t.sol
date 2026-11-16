// SPDX-License-Identifier: MIT
pragma solidity 0.8.26;

import {Test, console} from "forge-std/Test.sol";
import {Hooks} from "@uniswap/v4-core/src/libraries/Hooks.sol";
import {TickMath} from "@uniswap/v4-core/src/libraries/TickMath.sol";
import {Currency} from "@uniswap/v4-core/src/types/Currency.sol";
import {HookArgs} from "../../src/interfaces/ITouchstoneFactory.sol";
import {IUniswapV3PoolOracle} from "../../src/interfaces/IUniswapV3PoolOracle.sol";
import {TouchstoneHook} from "../../src/TouchstoneHook.sol";
import {Tier} from "../../src/types/Tier.sol";
import {Tiers} from "../../script/Tiers.sol";
import {Addresses} from "../../script/Addresses.sol";

/// @notice G-1b on a mainnet fork: the gas of the reference read against USDC/WETH 0.05% and of a snapshot that
///         refreshes, at the pool's observed cardinality and with the cardinality forced to 65,535.
///         Runs with `FOUNDRY_PROFILE=fork forge test --mp test/fork` (optionally `FORK_BLOCK=<n>`); skips when
///         MAINNET_RPC_URL is unset.
contract HookGasForkTest is Test {
    uint160 internal constant FLAGS = Hooks.BEFORE_INITIALIZE_FLAG | Hooks.BEFORE_SWAP_FLAG | Hooks.BEFORE_DONATE_FLAG;
    address internal constant VAULT = address(0x5A17);

    IUniswapV3PoolOracle internal refPool = IUniswapV3PoolOracle(Addresses.MAINNET_REF_POOL_USDC_WETH_500);
    TouchstoneHook internal hook;
    Tier internal tier;

    function _setUpFork() internal returns (bool) {
        string memory url = vm.envOr("MAINNET_RPC_URL", string(""));
        if (bytes(url).length == 0) {
            vm.skip(true);
            return false;
        }
        // FORK_BLOCK pins the fork so that Foundry's RPC cache serves repeated runs (public RPCs rate-limit)
        uint256 fb = vm.envOr("FORK_BLOCK", uint256(0));
        if (fb == 0) vm.createSelectFork(url);
        else vm.createSelectFork(url, fb);
        (Tier memory t,) = Tiers.mainnet();
        tier = t;
        (uint160 sqrtRef,,,,,,) = refPool.slot0();
        HookArgs memory a = HookArgs({
            poolManager: Addresses.MAINNET_POOL_MANAGER,
            vault: VAULT,
            currency0: Currency.wrap(refPool.token0()),
            currency1: Currency.wrap(refPool.token1()),
            fee: 500,
            tickSpacing: 10,
            seedSqrtPriceX96: sqrtRef,
            tier: t
        });
        address at = address(uint160(0x7777 << 100) | FLAGS);
        deployCodeTo("TouchstoneHook.sol:TouchstoneHook", abi.encode(a), at);
        hook = TouchstoneHook(at);
        hook.snapshotIfNewBlock();
        vm.prank(VAULT);
        hook.armAtSeed();
        return true;
    }

    uint16 internal _n;
    uint32 internal _newestTs;
    int24 internal _tick;

    /// @dev Observation `j` of a full synthetic ring of `_n` observations, 12 s apart, the newest (j = n - 1) at
    ///      `_newestTs`, at the pool's current tick, initialized.
    function _obsWord(uint256 j) internal view returns (bytes32) {
        uint32 ts = uint32(_newestTs - (_n - 1 - j) * 12);
        int56 cum = int56(int256(uint256(ts))) * int56(_tick);
        return bytes32(uint256(ts) | (uint256(uint56(cum)) << 32) | (uint256(1) << 248));
    }

    function _put(uint256 j) internal {
        vm.store(address(refPool), bytes32(uint256(8) + j), _obsWord(j));
    }

    /// @dev Makes the reference's oracle a full ring of `n` observations (index = n - 1, cardinality = n) as far as
    ///      any read of `targets` can see: it writes the newest and oldest slots and every slot v3's binary search
    ///      visits for each target (the search is replayed here on the same synthetic timestamps). The gas of a read
    ///      is then that of a full ring (the worst case: anyone can raise the cardinality and ordinary swaps fill it),
    ///      without writing 65,535 slots through the fork's RPC.
    function _fillRing(uint16 n, uint32 newestTs, uint32[] memory targets) internal {
        uint256 s0 = uint256(vm.load(address(refPool), 0));
        (_n, _newestTs, _tick) = (n, newestTs, int24(uint24(s0 >> 160)));
        s0 &= ~(uint256(0xffffffffffff) << 184); // observationIndex, observationCardinality, observationCardinalityNext
        s0 |= (uint256(n - 1) << 184) | (uint256(n) << 200) | (uint256(n) << 216);
        vm.store(address(refPool), 0, bytes32(s0));
        _put(n - 1);
        _put(0);
        for (uint256 t; t < targets.length; ++t) {
            uint256 l; // the oldest is (index + 1) % n = 0
            uint256 r = n - 1;
            while (true) {
                uint256 i = (l + r) / 2;
                _put(i % n);
                _put((i + 1) % n);
                uint32 before = uint32(uint256(_obsWord(i % n)));
                uint32 after_ = uint32(uint256(_obsWord((i + 1) % n)));
                bool atOrAfter = before <= targets[t];
                if (atOrAfter && targets[t] <= after_) break;
                if (!atOrAfter) r = i - 1;
                else l = i + 1;
            }
        }
    }

    /// @dev The ring is full (the slot after the newest is initialized) and `oldestTarget` lies inside it, before the
    ///      newest observation, so both ends of a read take the binary search.
    function _assertWorstPath(uint16 n, uint32 oldestTarget, uint32 newestTarget) internal view {
        uint256 newest = uint256(vm.load(address(refPool), bytes32(uint256(8) + n - 1)));
        uint256 next = uint256(vm.load(address(refPool), bytes32(uint256(8) + (uint256(n - 1) + 1) % n)));
        assertTrue(next >> 248 == 1, "ring full: the slot after the newest is initialized");
        assertLt(newestTarget, uint32(newest), "both ends precede the newest observation");
        assertGt(oldestTarget, uint32(next), "both ends lie inside the ring");
    }

    /// @dev Measures, on a full ring of `n` observations, the cold gas of the reference reads at three windows: a
    ///      typical one, the anchored read that `script/oracle_worst_window.py` finds worst (`sWorst` seconds into the
    ///      anchor; it is also the read of the refresh block measured below) and the worst markout read `(mA, mB]`
    ///      (`tryWindow` at a late settlement). Asserts REF_GAS_CAP >= 1.5x the largest and the refresh block's bound.
    function _measure(uint16 n, uint32 sWorst, uint32 mA, uint32 mB, string memory label) internal {
        uint256 tk = (vm.getBlockTimestamp() / tier.GRID_SEC + 1) * tier.GRID_SEC;
        vm.warp(tk + sWorst); // the refresh block reads the TWAP ending at the anchor, sWorst seconds ago
        vm.roll(vm.getBlockNumber() + tier.GRID);
        uint32 nowTs = uint32(vm.getBlockTimestamp());
        uint32[2][3] memory w = [[tier.REF_WINDOW + 120, uint32(120)], [sWorst + tier.REF_WINDOW, sWorst], [mA, mB]];
        uint32[] memory targets = new uint32[](6);
        for (uint256 i; i < 3; ++i) {
            (targets[2 * i], targets[2 * i + 1]) = (nowTs - w[i][0], nowTs - w[i][1]);
        }
        _fillRing(n, nowTs - 1, targets);
        uint256 worst;
        uint32[] memory ago = new uint32[](2);
        for (uint256 i; i < 3; ++i) {
            (ago[0], ago[1]) = (w[i][0], w[i][1]);
            _assertWorstPath(n, nowTs - ago[0], nowTs - ago[1]);
            vm.cool(address(refPool));
            uint256 g = gasleft();
            refPool.observe(ago);
            uint256 read = g - gasleft();
            console.log(label, ago[0], ago[1], read);
            if (read > worst) worst = read;
            vm.cool(address(refPool));
            (bool ok,) = hook.tryWindow(ago[0], ago[1]);
            assertTrue(ok, "the capped read succeeds");
        }
        vm.cool(address(refPool));
        vm.cool(address(hook));
        uint256 g0 = gasleft();
        hook.refreshRef();
        uint256 refreshBlock = g0 - gasleft();
        console.log(label, "worst read, refresh block", worst, refreshBlock);
        assertEq(hook.obs().refCachedAt, vm.getBlockNumber());
        assertLe(worst * 3 / 2, tier.REF_GAS_CAP, "REF_GAS_CAP is at least 1.5x the worst read");
        assertLe(refreshBlock, uint256(tier.REFRESH_GAS_MIN) + 20_000);
    }

    function test_G_1b__refreshBlockGasOnMainnetFork() public {
        if (!_setUpFork()) return;
        (,,, uint16 card,,,) = refPool.slot0();
        // windows from `python3 script/oracle_worst_window.py <n>` (mainnet tier); 723 is the cardinality observed
        // when they were computed (other cardinalities still measure valid, possibly milder, windows)
        _measure(card, 2124, 4416, 4296, "observed cardinality, full ring:");
        _measure(65_535, 3048, 393_312, 393_192, "cardinality 65535, full ring:");
    }
}
