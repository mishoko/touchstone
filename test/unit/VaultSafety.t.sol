// SPDX-License-Identifier: MIT
pragma solidity 0.8.26;

import {IPoolManager} from "@uniswap/v4-core/src/interfaces/IPoolManager.sol";
import {IUnlockCallback} from "@uniswap/v4-core/src/interfaces/callback/IUnlockCallback.sol";
import {Currency, CurrencyLibrary} from "@uniswap/v4-core/src/types/Currency.sol";
import {CustomRevert} from "@uniswap/v4-core/src/libraries/CustomRevert.sol";
import {IERC20} from "@openzeppelin/contracts/token/ERC20/IERC20.sol";
import {ReentrancyGuardTransient} from "@openzeppelin/contracts/utils/ReentrancyGuardTransient.sol";
import {VaultBase, ClaimDonor} from "../utils/VaultBase.sol";
import {MockToken} from "../utils/Stubs.sol";
import {TouchstoneVault} from "../../src/TouchstoneVault.sol";
import {Holdings} from "../../src/types/Holdings.sol";
import {Entry} from "../../src/types/Entry.sol";

/// @notice A token that calls an arbitrary target on every balance update while armed (an ERC-777-style hook), and
///         bubbles the target's revert.
contract CallbackToken is MockToken {
    address public target;
    bytes public data;
    bool public armed;

    constructor(string memory n, uint8 d) MockToken(n, d) {}

    function arm(address t, bytes calldata d) external {
        (target, data, armed) = (t, d, true);
    }

    function _update(address from, address to, uint256 value) internal override {
        super._update(from, to, value);
        if (armed) {
            armed = false;
            (bool ok, bytes memory ret) = target.call(data);
            if (!ok) {
                assembly ("memory-safe") {
                    revert(add(ret, 0x20), mload(ret))
                }
            }
        }
    }
}

/// @notice Calls the vault's `unlockCallback` from inside its own `unlock` (a foreign unlock).
contract ForeignUnlocker is IUnlockCallback {
    IPoolManager internal immutable manager;
    address internal immutable vault;

    constructor(IPoolManager m, address v) {
        (manager, vault) = (m, v);
    }

    function go(bytes calldata payload) external {
        manager.unlock(payload);
    }

    function unlockCallback(bytes calldata payload) external returns (bytes memory) {
        return TouchstoneVault(vault).unlockCallback(payload);
    }
}

contract VaultReentrancyTest is VaultBase {
    function _newToken(string memory n, uint8 d) internal override returns (MockToken) {
        return new CallbackToken(n, d);
    }

    function _cb() internal view returns (CallbackToken) {
        return CallbackToken(address(token0));
    }

    function test_U_reent__tokenCallbackCannotReenterDeposit() public {
        _fundUser(alice, 10e18);
        _cb().arm(vault, abi.encodeCall(TouchstoneVault.deposit, (1, type(uint256).max, type(uint256).max, alice)));
        vm.prank(alice);
        vm.expectRevert(ReentrancyGuardTransient.ReentrancyGuardReentrantCall.selector);
        v.deposit(1e18, type(uint256).max, type(uint256).max, alice);
    }

    function test_U_reent__tokenCallbackCannotReenterWithdraw() public {
        _fundUser(alice, 10e18);
        _deposit(alice, 1e24);
        // the payout's transfer from the PoolManager to `alice` calls back into withdraw
        _cb().arm(vault, abi.encodeCall(TouchstoneVault.withdraw, (1, 0, 0, alice)));
        // the PoolManager wraps the token's revert: the inner reason is the vault's reentrancy guard
        vm.prank(alice);
        vm.expectRevert(
            abi.encodeWithSelector(
                CustomRevert.WrappedError.selector,
                address(token0),
                IERC20.transfer.selector,
                abi.encodeWithSelector(ReentrancyGuardTransient.ReentrancyGuardReentrantCall.selector),
                abi.encodeWithSelector(CurrencyLibrary.ERC20TransferFailed.selector)
            )
        );
        v.withdraw(1e23, 0, 0, alice);
    }

    function test_U_reent__controlUnarmedCallbackTokenWorks() public {
        _fundUser(alice, 10e18);
        _deposit(alice, 1e24);
        _withdraw(alice, 1e23);
        assertEq(v.balanceOf(alice), 9e23);
    }

    function _payloads() internal view returns (bytes[] memory p) {
        p = new bytes[](3);
        // withdraw the bond-holding vault's shares to the attacker; deposit with alice as the payer; re-seed
        p[0] = abi.encode(Entry.Withdraw, vault, abi.encode(uint256(1e23), uint256(0), uint256(0), bob));
        p[1] = abi.encode(Entry.Deposit, alice, abi.encode(uint256(1e23), type(uint256).max, type(uint256).max, bob));
        p[2] = abi.encode(Entry.Seed, address(0), abi.encode(uint256(1), uint256(1), uint256(0), uint256(0)));
    }

    function test_U_reent__unlockCallbackFromAnEoaReverts() public {
        _fundUser(alice, 10e18);
        bytes[] memory p = _payloads();
        for (uint256 i; i < p.length; ++i) {
            vm.prank(bob);
            vm.expectRevert(TouchstoneVault.OnlyPoolManager.selector);
            v.unlockCallback(p[i]);
        }
    }

    function test_U_reent__unlockCallbackFromAForeignUnlockReverts() public {
        _fundUser(alice, 10e18);
        ForeignUnlocker f = new ForeignUnlocker(manager, vault);
        bytes[] memory p = _payloads();
        for (uint256 i; i < p.length; ++i) {
            vm.expectRevert(TouchstoneVault.OnlyPoolManager.selector);
            f.go(p[i]);
        }
        assertEq(v.balanceOf(bob), 0);
        assertEq(token0.balanceOf(alice), 10e18, "alice's approval was not spent");
    }
}

contract VaultFlowTest is VaultBase {
    /// @notice U-1b: a deposit or a withdrawal in a new block snapshots the hook first (its `_lastSnapBlock`, slot 0,
    ///         low 40 bits, equals the block).
    function test_U_1b__depositAndWithdrawSnapshotTheHookFirst() public {
        _fundUser(alice, 10e18);
        advance(3);
        assertLt(_lastSnap(), block.number, "control: no snapshot yet in this block");
        _deposit(alice, 1e23);
        assertEq(_lastSnap(), block.number, "deposit");
        advance(2);
        assertLt(_lastSnap(), block.number);
        _withdraw(alice, 1e22);
        assertEq(_lastSnap(), block.number, "withdraw");
    }

    function _lastSnap() internal view returns (uint256) {
        return uint256(vm.load(hook, bytes32(0))) & type(uint40).max;
    }

    // ------------------------------------------------------------------ M-7 / I-11: unsolicited transfers

    /// @notice Donations of tokens, claims or shares to the vault change neither its buckets nor NAV0 nor any
    ///         holder's payout.
    function test_I_11__unsolicitedTransfersDoNotMoveNav(uint96 t0, uint96 c1, uint96 sh) public {
        _fundUser(alice, 1e24);
        _fundUser(bob, 1e24);
        _deposit(alice, 3e23);
        _deposit(bob, 2e23);
        uint256 snap = vm.snapshotState();
        (uint256 base0, uint256 base1) = _withdraw(alice, 1e23);
        vm.revertToState(snap);

        Holdings memory h = v.holdings();
        uint256 nav0 = v.nav0();
        ClaimDonor donor = new ClaimDonor(manager);
        token0.mint(address(donor), c1);
        token1.mint(address(this), t0);
        token1.transfer(vault, t0); // a plain ERC-20 transfer
        donor.donate(token0, vault, c1); // ERC-6909 claims minted to the vault
        sh = uint96(bound(sh, 0, v.balanceOf(bob)));
        vm.prank(bob);
        v.transfer(vault, sh); // shares sent to the vault

        Holdings memory h2 = v.holdings();
        assertEq(h2.idle0, h.idle0, "idle0");
        assertEq(h2.idle1, h.idle1, "idle1");
        assertEq(v.nav0(), nav0, "NAV0");
        (uint256 o0, uint256 o1) = _withdraw(alice, 1e23);
        assertEq(o0, base0, "payout token0");
        assertEq(o1, base1, "payout token1");
    }

    /// @notice M-7: the donation (first-depositor) attack loses money: the attacker deposits, donates to the vault,
    ///         a victim deposits, the attacker withdraws. Positive control: the same sequence against a vault that
    ///         prices shares from its token balances without dead shares profits.
    function test_M_7__donationAttackLosesMoney(uint64 donation) public {
        uint256 d = bound(donation, 1e6, 1e21);
        address attacker = makeAddr("attacker");
        _fundUser(attacker, 1e24);
        _fundUser(alice, 1e24);
        uint256 start0 = token0.balanceOf(attacker);
        uint256 start1 = token1.balanceOf(attacker);
        _deposit(attacker, 1);
        vm.startPrank(attacker);
        token0.transfer(vault, d);
        token1.transfer(vault, d);
        vm.stopPrank();
        _deposit(alice, 1e24);
        _withdraw(attacker, v.balanceOf(attacker));
        uint256 end0 = token0.balanceOf(attacker);
        uint256 end1 = token1.balanceOf(attacker);
        assertLt(end0 + end1, start0 + start1, "the attack loses money");
        assertLe(end0, start0 - d);
    }

    function test_M_7__controlNaiveVaultProfits() public {
        NaiveVault nv = new NaiveVault(token0);
        address attacker = makeAddr("attacker");
        token0.mint(attacker, 1e21 + 1);
        token0.mint(alice, 1e21);
        vm.startPrank(attacker);
        token0.approve(address(nv), type(uint256).max);
        nv.deposit(1);
        token0.transfer(address(nv), 1e21);
        vm.stopPrank();
        vm.startPrank(alice);
        token0.approve(address(nv), type(uint256).max);
        nv.deposit(1e21 - 1); // rounds to 0 shares
        vm.stopPrank();
        vm.prank(attacker);
        nv.redeem(1);
        assertGt(token0.balanceOf(attacker), 1e21 + 1, "control: the attack profits without the defences");
    }
}

/// @notice A single-token vault that prices shares from its own token balance and has no seed or dead shares.
contract NaiveVault {
    MockToken internal immutable token;
    mapping(address => uint256) public shares;
    uint256 public supply;

    constructor(MockToken t) {
        token = t;
    }

    function deposit(uint256 assets) external {
        uint256 bal = token.balanceOf(address(this));
        uint256 s = supply == 0 ? assets : assets * supply / bal;
        token.transferFrom(msg.sender, address(this), assets);
        shares[msg.sender] += s;
        supply += s;
    }

    function redeem(uint256 s) external {
        uint256 out = s * token.balanceOf(address(this)) / supply;
        shares[msg.sender] -= s;
        supply -= s;
        token.transfer(msg.sender, out);
    }
}
