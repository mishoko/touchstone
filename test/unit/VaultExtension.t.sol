// SPDX-License-Identifier: MIT
pragma solidity 0.8.26;

import {Test, Vm} from "forge-std/Test.sol";
import {IPoolManager} from "@uniswap/v4-core/src/interfaces/IPoolManager.sol";
import {IUnlockCallback} from "@uniswap/v4-core/src/interfaces/callback/IUnlockCallback.sol";
import {Currency, CurrencyLibrary} from "@uniswap/v4-core/src/types/Currency.sol";
import {CustomRevert} from "@uniswap/v4-core/src/libraries/CustomRevert.sol";
import {IERC20} from "@openzeppelin/contracts/token/ERC20/IERC20.sol";
import {ReentrancyGuardTransient} from "@openzeppelin/contracts/utils/ReentrancyGuardTransient.sol";
import {VaultBase} from "../utils/VaultBase.sol";
import {MockToken} from "../utils/Stubs.sol";
import {CallbackToken} from "./VaultSafety.t.sol";
import {MockVaultFactory, AckRecorder} from "../utils/ScriptedVault.sol";
import {VaultHarness} from "../utils/VaultHarness.sol";
import {Tiers} from "../../script/Tiers.sol";
import {HookMining} from "../../script/Mine.s.sol";
import {Tier} from "../../src/types/Tier.sol";
import {V} from "../../src/types/Bank.sol";
import {Entry} from "../../src/types/Entry.sol";
import {PreSwap} from "../../src/types/PreSwap.sol";
import {ITouchstoneFactory} from "../../src/interfaces/ITouchstoneFactory.sol";
import {TouchstoneVault} from "../../src/TouchstoneVault.sol";
import {TouchstoneVaultExtension} from "../../src/TouchstoneVaultExtension.sol";
import {VaultCommon} from "../../src/TouchstoneVaultBase.sol";
import {IVaultEntries, ITouchstoneVaultExtension} from "../../src/interfaces/ITouchstoneVaultExtension.sol";

/// @notice Calls a deposit and then a withdrawal through the vault in one transaction (the lock is released between).
contract TwoCalls {
    function go(address vault, address t0, address t1, uint256 s) external returns (uint256 d0, uint256 d1, uint256 w0, uint256 w1) {
        IERC20(t0).approve(vault, type(uint256).max);
        IERC20(t1).approve(vault, type(uint256).max);
        (d0, d1) = IVaultEntries(vault).deposit(s, type(uint256).max, type(uint256).max, address(this));
        (w0, w1) = IVaultEntries(vault).withdraw(s, 0, 0, address(this));
    }
}

/// @notice A foreign contract that, inside its own `unlock` (so `msg.sender == PoolManager` on its calls), calls the
///         vault's `unlockCallback` and the extension's `dispatch`, or delegatecalls `dispatch` (U-ext (c)).
contract ForeignDispatcher is IUnlockCallback {
    IPoolManager internal immutable manager;
    address internal immutable vault;
    address internal immutable ext;
    uint8 internal _mode;
    bytes public lastRevert;
    bool public lastOk;

    constructor(IPoolManager m, address v, address x) {
        (manager, vault, ext) = (m, v, x);
    }

    function go(uint8 mode) external {
        _mode = mode;
        manager.unlock("");
    }

    function unlockCallback(bytes calldata) external returns (bytes memory) {
        bytes memory payload = abi.encode(Entry.Withdraw, vault, abi.encode(uint256(1e23), uint256(0), uint256(0), address(this)));
        bytes memory d = abi.encodeCall(ITouchstoneVaultExtension.dispatch, (Entry.Withdraw, vault, abi.encode(uint256(1e23), uint256(0), uint256(0), address(this))));
        bool ok;
        bytes memory ret;
        if (_mode == 0) (ok, ret) = vault.call(abi.encodeCall(IUnlockCallback.unlockCallback, (payload)));
        else if (_mode == 1) (ok, ret) = ext.call(d);
        else (ok, ret) = ext.delegatecall(d);
        (lastOk, lastRevert) = (ok, ret);
        return "";
    }
}

/// @notice U-ext (spec §4.12, §9.1): the per-vault extension's routing and binding. Each row has its control.
contract VaultExtensionTest is VaultBase {
    address internal ext;

    function setUp() public virtual override {
        super.setUp();
        ext = TouchstoneVault(vault).EXTENSION();
    }

    function _ve() internal view returns (IVaultEntries) {
        return IVaultEntries(vault);
    }

    // ------------------------------------------------------------------ (a) the fallback's allow-list

    /// @notice (a) The fallback forwards only the allow-list: `dispatch`, the extension's getters, a random selector, a
    ///         short call and empty calldata revert `UnknownSelector`. Control: an allow-listed selector succeeds.
    function test_U_ext__fallbackRefusesEverySelectorOutsideTheAllowList() public {
        bytes[] memory calls = new bytes[](8);
        calls[0] = abi.encodeCall(ITouchstoneVaultExtension.dispatch, (Entry.Withdraw, alice, ""));
        calls[1] = abi.encodeCall(ITouchstoneVaultExtension.VAULT, ());
        calls[2] = abi.encodeCall(ITouchstoneVaultExtension.SELF, ());
        calls[3] = abi.encodeCall(ITouchstoneVaultExtension.CONFIG_HASH, ());
        calls[4] = abi.encodeWithSelector(bytes4(0xdeadbeef), uint256(1));
        calls[5] = "";
        calls[6] = hex"fad3cc"; // three bytes of deposit's selector
        // a P4 record-write selector (not yet built; it is never on the allow-list)
        calls[7] = abi.encodeWithSignature("roll(uint32)", uint32(1));
        for (uint256 i; i < calls.length; ++i) {
            (bool ok, bytes memory ret) = vault.call(calls[i]);
            assertFalse(ok, "refused");
            assertEq(ret, abi.encodeWithSelector(VaultCommon.UnknownSelector.selector), "UnknownSelector");
        }
        // control: an allow-listed selector is forwarded and succeeds (the P4 entries included)
        _fundUser(alice, 10e18);
        _deposit(alice, 1e22);
        assertEq(v.balanceOf(alice), 1e22);
        _ve().poke();
        _ve().processBreach();
        vm.startPrank(curator);
        _ve().claim();
        _ve().topUpBond(0);
        _ve().retire();
        vm.roll(block.number + V * tier.EPOCH);
        vm.warp(block.timestamp + V * tier.EPOCH * blocktime);
        _ve().releaseBond();
        vm.stopPrank();
        assertGt(v.balanceOf(curator), 0, "the whole allow-list forwarded (the bond released)");
    }

    // ------------------------------------------------------------------ (b) direct calls

    /// @notice (b) A direct call to each moved entry point and to `dispatch` on the extension reverts `NotDelegated`.
    ///         Control: the same entry points through the vault succeed.
    function test_U_ext__directCallsToTheExtensionRevert() public {
        _fundUser(alice, 10e18);
        vm.startPrank(alice);
        token0.approve(ext, type(uint256).max);
        token1.approve(ext, type(uint256).max);
        vm.expectRevert(VaultCommon.NotDelegated.selector);
        IVaultEntries(ext).deposit(1e22, type(uint256).max, type(uint256).max, alice);
        vm.expectRevert(VaultCommon.NotDelegated.selector);
        IVaultEntries(ext).withdraw(1, 0, 0, alice);
        vm.stopPrank();
        vm.prank(address(factory));
        vm.expectRevert(VaultCommon.NotDelegated.selector);
        IVaultEntries(ext).seed(1, 1, alice, 0, 0);
        // the six P4 entries, as anyone and as the curator
        for (uint256 who; who < 2; ++who) {
            vm.startPrank(who == 0 ? alice : curator);
            vm.expectRevert(VaultCommon.NotDelegated.selector);
            IVaultEntries(ext).claim();
            vm.expectRevert(VaultCommon.NotDelegated.selector);
            IVaultEntries(ext).topUpBond(1);
            vm.expectRevert(VaultCommon.NotDelegated.selector);
            IVaultEntries(ext).retire();
            vm.expectRevert(VaultCommon.NotDelegated.selector);
            IVaultEntries(ext).releaseBond();
            vm.expectRevert(VaultCommon.NotDelegated.selector);
            IVaultEntries(ext).poke();
            vm.expectRevert(VaultCommon.NotDelegated.selector);
            IVaultEntries(ext).processBreach();
            vm.stopPrank();
        }
        vm.prank(address(manager));
        vm.expectRevert(VaultCommon.NotDelegated.selector);
        ITouchstoneVaultExtension(ext).dispatch(Entry.Deposit, alice, abi.encode(uint256(1), uint256(0), uint256(0), alice));
        // control: through the vault
        (uint256 a0, uint256 a1) = _deposit(alice, 1e22);
        assertGt(a0 + a1, 0);
        _withdraw(alice, 1e22);
        assertEq(v.balanceOf(alice), 0);
    }

    // ------------------------------------------------------------------ (c) a foreign unlock

    /// @notice (c) Inside a foreign contract's own `unlock`, calling the vault's `unlockCallback` reverts
    ///         `OnlyPoolManager` and calling the extension's `dispatch` reverts `NotDelegated`. Control: the foreign
    ///         contract delegatecalling `dispatch` runs on its own storage only: the vault's storage, supply and
    ///         balances are unchanged.
    function test_U_ext__aForeignUnlockCannotReachTheMovedFlows() public {
        _fundUser(alice, 10e18);
        _deposit(alice, 1e23);
        ForeignDispatcher f = new ForeignDispatcher(manager, vault, ext);
        f.go(0);
        assertFalse(f.lastOk());
        assertEq(f.lastRevert(), abi.encodeWithSelector(VaultCommon.OnlyPoolManager.selector));
        f.go(1);
        assertFalse(f.lastOk());
        assertEq(f.lastRevert(), abi.encodeWithSelector(VaultCommon.NotDelegated.selector));
        // control: a delegatecall from the foreign contract passes both checks but acts on the foreign storage
        bytes32[40] memory before;
        for (uint256 i; i < 40; ++i) {
            before[i] = vm.load(vault, bytes32(i));
        }
        uint256 supply = v.totalSupply();
        uint256 b0 = token0.balanceOf(address(manager));
        f.go(2);
        // the call got past both checks (the SELF refusal and the PoolManager check) and failed inside the flow's body,
        // on the foreign contract's own storage (its zero supply), not on a guard
        assertFalse(f.lastOk(), "the body ran on the foreign storage and reverted there");
        assertTrue(
            keccak256(f.lastRevert()) != keccak256(abi.encodeWithSelector(VaultCommon.NotDelegated.selector))
                && keccak256(f.lastRevert()) != keccak256(abi.encodeWithSelector(VaultCommon.OnlyPoolManager.selector)),
            "past both checks"
        );
        for (uint256 i; i < 40; ++i) {
            assertEq(vm.load(vault, bytes32(i)), before[i], "vault storage unchanged");
        }
        assertEq(v.totalSupply(), supply);
        assertEq(token0.balanceOf(address(manager)), b0);
        assertEq(token0.balanceOf(address(f)) + token1.balanceOf(address(f)), 0, "nothing paid out");
    }

    // ------------------------------------------------------------------ (f) seed

    /// @notice (f) `seed` through the fallback by a non-factory caller, or by the factory a second time, reverts
    ///         `OnlyFactory`. Control (and D-1's seed path): the factory's one seed ran at once, without the prologue,
    ///         before the vault was seeded.
    function test_U_ext__seedOnlyOnceAndOnlyByTheFactory() public {
        assertEq(v.totalSupply(), v.S_SEED(), "the factory's seed ran");
        vm.prank(alice);
        vm.expectRevert(VaultCommon.OnlyFactory.selector);
        _ve().seed(1e18, 1e18, alice, 0, 0);
        vm.prank(address(factory));
        vm.expectRevert(VaultCommon.OnlyFactory.selector);
        _ve().seed(1e18, 1e18, alice, 0, 0);
    }

    // ------------------------------------------------------------------ returns and the lock

    /// @notice The moved flows return their ABI outputs through the fallback: `deposit` returns what the caller paid and
    ///         `withdraw` what it received.
    function test_U_ext__depositAndWithdrawReturnTheBalanceDeltas() public {
        _fundUser(alice, 10e18);
        uint256 t0 = token0.balanceOf(alice);
        uint256 t1 = token1.balanceOf(alice);
        (uint256 a0, uint256 a1) = _deposit(alice, 3e23);
        assertEq(t0 - token0.balanceOf(alice), a0, "deposit a0");
        assertEq(t1 - token1.balanceOf(alice), a1, "deposit a1");
        assertGt(a0 + a1, 0);
        t0 = token0.balanceOf(alice);
        t1 = token1.balanceOf(alice);
        (uint256 o0, uint256 o1) = _withdraw(alice, 1e23);
        assertEq(token0.balanceOf(alice) - t0, o0, "withdraw o0");
        assertEq(token1.balanceOf(alice) - t1, o1, "withdraw o1");
        assertGt(o0 + o1, 0);
    }

    /// @notice The moved flows' events are emitted from the vault's address (the extension's code runs by
    ///         delegatecall), with the flow's amounts; none comes from the extension.
    function test_U_ext__movedFlowEventsComeFromTheVault() public {
        _fundUser(alice, 10e18);
        vm.recordLogs();
        (uint256 a0, uint256 a1) = _deposit(alice, 3e23);
        (uint256 o0, uint256 o1) = _withdraw(alice, 1e23);
        Vm.Log[] memory logs = vm.getRecordedLogs();
        bytes32 tDep = keccak256("Deposit(address,uint256,uint256,uint256)");
        bytes32 tWd = keccak256("Withdraw(address,uint256,uint256,uint256)");
        uint256 seen;
        for (uint256 i; i < logs.length; ++i) {
            assertTrue(logs[i].emitter != ext, "nothing is emitted from the extension's address");
            bytes32 t = logs[i].topics[0];
            if (t != tDep && t != tWd) continue;
            assertEq(logs[i].emitter, vault, "emitted from the vault");
            assertEq(logs[i].topics[1], bytes32(uint256(uint160(alice))));
            if (t == tDep) assertEq(logs[i].data, abi.encode(a0, a1, uint256(3e23)), "Deposit fields");
            else assertEq(logs[i].data, abi.encode(uint256(1e23), o0, o1), "Withdraw fields");
            ++seen;
        }
        assertEq(seen, 2, "one Deposit and one Withdraw");
    }

    /// @notice (d) One transaction calling `deposit` then `withdraw` through the vault succeeds: the fallback releases
    ///         the lock on return.
    function test_U_ext__depositThenWithdrawInOneTransaction() public {
        TwoCalls c = new TwoCalls();
        token0.mint(address(c), 10e18);
        token1.mint(address(c), 10e18);
        (uint256 d0, uint256 d1, uint256 w0, uint256 w1) = c.go(vault, address(token0), address(token1), 1e23);
        assertGt(d0 + d1, 0);
        assertGt(w0 + w1, 0);
        assertEq(v.balanceOf(address(c)), 0);
    }

    // ------------------------------------------------------------------ the factory's binding

    /// @notice The factory rejects an extension code other than the one whose hash it holds (`CodeHash`). Control: the
    ///         right code deploys (`setUp`), and the vault and its extension point at each other.
    function test_U_ext__factoryRejectsAWrongExtensionCode() public {
        assertEq(ITouchstoneVaultExtension(ext).VAULT(), vault);
        assertEq(ext.code.length > 0, true);
        address other = makeAddr("other");
        bytes memory bad = abi.encodePacked(_extCode(), hex"00");
        vm.prank(other);
        vm.expectRevert(ITouchstoneFactory.CodeHash.selector);
        factory.deploy(_vaultCode(), bad, _hookCode(), hookArgs, bytes32(uint256(7)), saltH, seedParams);
    }

    /// @notice The extension's salt derives from the vault's caller-bound salt, so its address is fixed by the vault's.
    function test_U_ext__extensionAddressDerivesFromTheVaultSalt() public view {
        bytes32 sv = HookMining.boundSalt(deployer, SALT_V);
        bytes32 se = keccak256(abi.encode(sv, "ext"));
        assertEq(ext, HookMining.create2Address(address(factory), se, keccak256(_extCode())));
    }
}

/// @notice (d) The shared lock: a token callback fired inside a moved flow cannot enter a resident entry point.
contract VaultExtensionLockTest is VaultBase {
    function _newToken(string memory n, uint8 d) internal override returns (MockToken) {
        return new CallbackToken(n, d);
    }

    function _cb() internal view returns (CallbackToken) {
        return CallbackToken(address(token0));
    }

    /// @notice A callback during `deposit` (a moved flow) into `settle` or `setPositions` (resident) reverts on the
    ///         shared lock. Control: the unarmed token deposits.
    function test_U_ext__aMovedFlowCannotReenterAResidentEntryPoint() public {
        _fundUser(alice, 10e18);
        bytes[5] memory inner = [
            abi.encodeCall(TouchstoneVault.settle, ()),
            abi.encodeCall(TouchstoneVault.setPositions, (int24(-600), int24(600), PreSwap(false, false, 0), uint160(0), type(uint256).max)),
            // moved entries through the fallback (the lock is shared)
            abi.encodeCall(IVaultEntries.poke, ()),
            abi.encodeCall(IVaultEntries.processBreach, ()),
            abi.encodeCall(IVaultEntries.claim, ()) // refused on the shared lock before its curator check
        ];
        for (uint256 i; i < 5; ++i) {
            _cb().arm(vault, inner[i]);
            vm.prank(alice);
            vm.expectRevert(ReentrancyGuardTransient.ReentrancyGuardReentrantCall.selector);
            IVaultEntries(vault).deposit(1e22, type(uint256).max, type(uint256).max, alice);
        }
        // control: the reverted calls left the token armed; re-arm it with a harmless call, then deposit
        _cb().arm(address(token1), abi.encodeCall(IERC20.totalSupply, ()));
        _deposit(alice, 1e22);
        assertEq(v.balanceOf(alice), 1e22);
    }
}

/// @notice The vault constructor's consistency checks with a mock factory: an extension with no code, built for
///         another vault, or under another configuration reverts `BadExtension`. Control: the matching pending deploys.
contract VaultExtensionBindingTest is Test {
    function _factory() internal returns (MockVaultFactory) {
        (Tier memory t,) = Tiers.testing();
        return new MockVaultFactory(address(new AckRecorder()), t);
    }

    function test_U_ext__vaultConstructorRejectsAMismatchedExtension() public {
        bytes memory code = type(VaultHarness).creationCode;
        bytes memory extCode = type(TouchstoneVaultExtension).creationCode;
        for (uint8 defect = 1; defect <= 3; ++defect) {
            MockVaultFactory f = _factory();
            vm.expectRevert(VaultCommon.BadExtension.selector);
            f.deployWith(code, extCode, defect);
        }
        // control
        address vlt = _factory().deployWith(code, extCode, 0);
        assertTrue(vlt.code.length > 0);
        assertEq(ITouchstoneVaultExtension(TouchstoneVault(vlt).EXTENSION()).VAULT(), vlt);
    }
}
