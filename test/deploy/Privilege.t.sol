// SPDX-License-Identifier: MIT
pragma solidity 0.8.26;

import {IHooks} from "@uniswap/v4-core/src/interfaces/IHooks.sol";
import {PoolKey} from "@uniswap/v4-core/src/types/PoolKey.sol";
import {Currency} from "@uniswap/v4-core/src/types/Currency.sol";
import {SwapParams} from "@uniswap/v4-core/src/types/PoolOperation.sol";
import {DeployBaseUnplaced} from "../utils/DeployBaseUnplaced.sol";
import {MockToken} from "../utils/Stubs.sol";
import {TouchstoneVault} from "../../src/TouchstoneVault.sol";
import {TouchstoneVaultExtension} from "../../src/TouchstoneVaultExtension.sol";
import {TouchstoneHook} from "../../src/TouchstoneHook.sol";
import {ITouchstoneHook} from "../../src/interfaces/ITouchstoneHook.sol";
import {IVaultEntries} from "../../src/interfaces/ITouchstoneVaultExtension.sol";
import {VaultCommon} from "../../src/TouchstoneVaultBase.sol";
import {PreSwap} from "../../src/types/PreSwap.sol";
import {Obs} from "../../src/types/Obs.sol";

/// @notice A token that calls a probe once, on its next `transferFrom` (the factory's pull to the vault, which runs
///         after the vault and the hook exist and before `seed`).
contract CallbackToken is MockToken {
    address public probe;

    constructor(string memory n, uint8 d) MockToken(n, d) {}

    function arm(address p) external {
        probe = p;
    }

    function transferFrom(address from, address to, uint256 value) public override returns (bool ok) {
        ok = super.transferFrom(from, to, value);
        address p = probe;
        if (p != address(0)) {
            probe = address(0);
            PreSeedProbe(p).run(to);
        }
    }
}

/// @notice Calls every vault entry point and the hook's vault-only entry point on a deployed but unseeded vault, and
///         records each outcome and the flags it sees.
contract PreSeedProbe {
    bytes4[7] public errs;
    bool[7] public reverted;
    uint256 public supply;
    uint40 public cachedAt;
    bool public emptyRange;
    bool public ran;

    function _call(uint256 i, address target, bytes memory data) internal {
        (bool ok, bytes memory ret) = target.call(data);
        reverted[i] = !ok;
        if (!ok && ret.length >= 4) errs[i] = bytes4(ret);
    }

    function run(address vault) external {
        ran = true;
        address hook = address(TouchstoneVault(vault).HOOK());
        supply = TouchstoneVault(vault).totalSupply();
        Obs memory o = ITouchstoneHook(hook).obs();
        (cachedAt, emptyRange) = (o.refCachedAt, o.emptyRange);
        _call(0, vault, abi.encodeCall(TouchstoneVault.settle, ()));
        _call(1, vault, abi.encodeCall(IVaultEntries.deposit, (1e18, type(uint256).max, type(uint256).max, address(this))));
        _call(2, vault, abi.encodeCall(IVaultEntries.withdraw, (1, 0, 0, address(this))));
        _call(3, vault, abi.encodeCall(TouchstoneVault.setPositions, (-600, 600, PreSwap(false, false, 0), 0, 0)));
        _call(4, vault, abi.encodeCall(TouchstoneVault.rebalanceSwap, (true, 1, 0)));
        _call(5, vault, abi.encodeCall(IVaultEntries.seed, (1e18, 1e18, address(this), 0, 0)));
        _call(6, hook, abi.encodeCall(ITouchstoneHook.armAtSeed, ()));
    }
}

/// @notice D-2 and D-3 (spec §9.1, §4.8): the production vault, extension and hook through the factory.
contract PrivilegeTest is DeployBaseUnplaced {
    address internal stranger = makeAddr("stranger");

    function _vaultCode() internal pure override returns (bytes memory) {
        return type(TouchstoneVault).creationCode;
    }

    function _extCode() internal pure override returns (bytes memory) {
        return type(TouchstoneVaultExtension).creationCode;
    }

    function _newToken(string memory n, uint8 d) internal override returns (MockToken) {
        return new CallbackToken(n, d);
    }

    function setUp() public override {
        _setUpEnv();
    }

    // ------------------------------------------------------------------ D-2

    /// @notice D-2: between the vault's creation and its `seed` (reached from the factory's token pull), every entry
    ///         point reverts: the flows and the settlement `NotSeeded`, the curator actions `OnlyCurator` (no curator
    ///         is set yet), `seed` from anyone but the factory `OnlyFactory`, and the hook's `armAtSeed` from anyone
    ///         but the vault `OnlyVault`; the flags read unseeded (supply 0, the reference cache empty, the range
    ///         EMPTY). Control: after the deployment the flags are set and a deposit succeeds.
    function test_D_2__preSeedEntryPointsRevertAndFlagsReadUnseeded() public {
        PreSeedProbe p = new PreSeedProbe();
        CallbackToken(address(token0)).arm(address(p));
        (vault, hook) = _deploy(deployer, SALT_V);
        assertTrue(p.ran(), "the probe ran inside the deployment");
        bytes4[7] memory want = [
            VaultCommon.NotSeeded.selector,
            VaultCommon.NotSeeded.selector,
            VaultCommon.NotSeeded.selector,
            VaultCommon.OnlyCurator.selector,
            VaultCommon.OnlyCurator.selector,
            VaultCommon.OnlyFactory.selector,
            ITouchstoneHook.OnlyVault.selector
        ];
        for (uint256 i; i < 7; ++i) {
            assertTrue(p.reverted(i), "reverted");
            assertEq(p.errs(i), want[i], "with its guard");
        }
        assertEq(p.supply(), 0, "no supply before seed");
        assertEq(p.cachedAt(), 0, "no reference cache before seed");
        assertTrue(p.emptyRange(), "EMPTY range");
        // control: seeded
        TouchstoneVault v = TouchstoneVault(vault);
        assertEq(v.totalSupply(), v.S_SEED(), "control: seeded");
        assertEq(ITouchstoneHook(hook).obs().refCachedAt, vm.getBlockNumber(), "control: cache written at seed");
        token0.mint(stranger, 1e24);
        token1.mint(stranger, 1e24);
        vm.startPrank(stranger);
        token0.approve(vault, type(uint256).max);
        token1.approve(vault, type(uint256).max);
        IVaultEntries(vault).deposit(1e18, type(uint256).max, type(uint256).max, stranger);
        vm.stopPrank();
        assertEq(v.balanceOf(stranger), 1e18, "control: a deposit after seed");
    }

    // ------------------------------------------------------------------ D-3

    function _expectFrom(address who, address target, bytes memory data, bytes4 err) internal {
        vm.prank(who);
        (bool ok, bytes memory ret) = target.call(data);
        assertFalse(ok, "reverts");
        assertEq(bytes4(ret), err, "with the role's guard");
    }

    /// @notice D-3: after seed no address holds an administrative power over the hook, the vault or the factory. The
    ///         deployer, the factory, a stranger and the curator are refused alike on the hook's vault-only entry
    ///         points (`OnlyVault`) and its self-only witness step (`OnlySelf`), the vault's PoolManager callback
    ///         (`OnlyPoolManager`), the second `seed` (`OnlyFactory`, the factory included); only the curator role
    ///         reaches the curator actions (the deployer and the factory get `OnlyCurator`). The ABI side (no function
    ///         beyond this table, no owner or admin entry point) is `test_D_3__abi_has_no_privileged_entry_point` in
    ///         `test/static/test_static.py`.
    function test_D_3__noPrivilegedEntryPointAfterSeed() public {
        (vault, hook) = _deploy(deployer, SALT_V);
        address cur = seedParams.curator;
        address[4] memory callers = [deployer, address(factory), stranger, cur];
        bytes[5] memory onlyVault = [
            abi.encodeCall(ITouchstoneHook.setVaultRange, (-600, 600, 0)),
            abi.encodeCall(ITouchstoneHook.armAtSeed, ()),
            abi.encodeCall(ITouchstoneHook.ackBreaches, (1)),
            abi.encodeCall(ITouchstoneHook.fundWitness, (1, 1)),
            abi.encodeCall(ITouchstoneHook.rollWitness, (1))
        ];
        for (uint256 c; c < callers.length; ++c) {
            for (uint256 i; i < onlyVault.length; ++i) {
                _expectFrom(callers[c], hook, onlyVault[i], ITouchstoneHook.OnlyVault.selector);
            }
            _expectFrom(
                callers[c], hook, abi.encodeCall(TouchstoneHook.witnessStep, (1)), ITouchstoneHook.OnlySelf.selector
            );
            _expectFrom(
                callers[c], vault, abi.encodeCall(TouchstoneVault.unlockCallback, ("")), VaultCommon.OnlyPoolManager.selector
            );
            _expectFrom(
                callers[c],
                vault,
                abi.encodeCall(IVaultEntries.seed, (1e18, 1e18, callers[c], 0, 0)),
                VaultCommon.OnlyFactory.selector
            );
            PoolKey memory k =
                PoolKey(Currency.wrap(address(token0)), Currency.wrap(address(token1)), FEE, TICK_SPACING, IHooks(hook));
            vm.prank(callers[c]);
            (bool ok,) = hook.call(
                abi.encodeCall(IHooks.beforeSwap, (callers[c], k, SwapParams(true, -1, 1), ""))
            );
            assertFalse(ok, "the PoolManager's callbacks refuse every other caller");
        }
        for (uint256 c; c < 3; ++c) {
            _expectFrom(
                callers[c],
                vault,
                abi.encodeCall(TouchstoneVault.setPositions, (-600, 600, PreSwap(false, false, 0), 0, 0)),
                VaultCommon.OnlyCurator.selector
            );
            _expectFrom(
                callers[c], vault, abi.encodeCall(TouchstoneVault.rebalanceSwap, (true, 1, 0)), VaultCommon.OnlyCurator.selector
            );
        }
        // control: the curator role passes the role check (it reverts later, on the action's own guards, not OnlyCurator)
        vm.prank(cur);
        (bool ok2, bytes memory ret) = vault.call(abi.encodeCall(TouchstoneVault.rebalanceSwap, (true, 1, 0)));
        assertTrue(ok2 || bytes4(ret) != VaultCommon.OnlyCurator.selector, "control: the curator passes the role check");
    }
}
