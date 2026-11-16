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
import {VaultView} from "../utils/VaultView.sol";


using VaultView for TouchstoneVault;
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

    /// @notice Every (callback entry, re-entered entry) pair among deposit and withdraw reverts on the guard.
    function test_U_reent__tokenCallbacksCannotReenterAnyEntryPoint() public {
        _fundUser(alice, 10e18);
        _deposit(alice, 1e24);
        bytes[2] memory inner = [
            abi.encodeCall(TouchstoneVault.deposit, (1, type(uint256).max, type(uint256).max, alice)),
            abi.encodeCall(TouchstoneVault.withdraw, (1, 0, 0, alice))
        ];
        bytes memory guard = abi.encodeWithSelector(ReentrancyGuardTransient.ReentrancyGuardReentrantCall.selector);
        for (uint256 i; i < 2; ++i) {
            // the callback fires on the deposit's payment (alice -> PoolManager): the guard's revert is bubbled
            _cb().arm(vault, inner[i]);
            vm.prank(alice);
            vm.expectRevert(guard);
            v.deposit(1e18, type(uint256).max, type(uint256).max, alice);
            // the callback fires on the withdrawal's payout (PoolManager -> alice): the PoolManager wraps it
            _cb().arm(vault, inner[i]);
            vm.prank(alice);
            vm.expectRevert(
                abi.encodeWithSelector(
                    CustomRevert.WrappedError.selector,
                    address(token0),
                    IERC20.transfer.selector,
                    guard,
                    abi.encodeWithSelector(CurrencyLibrary.ERC20TransferFailed.selector)
                )
            );
            v.withdraw(1e23, 0, 0, alice);
        }
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
    /// @notice F-3, the vault half: the shares the vault holds cover every bank bucket, and shares sent to the vault
    ///         unsolicited are inert (they do not enter any bucket).
    function test_F_3__vaultHeldSharesCoverTheBank(uint96 gift, uint96 s1, uint96 s2) public {
        _fundUser(alice, 1e25);
        _fundUser(bob, 1e25);
        _deposit(alice, bound(s1, 1, 1e25));
        gift = uint96(bound(gift, 0, v.balanceOf(alice)));
        vm.prank(alice);
        v.transfer(vault, gift);
        _deposit(bob, bound(s2, 1, 1e25));
        if (v.balanceOf(alice) / 2 > 0) _withdraw(alice, v.balanceOf(alice) / 2);
        (uint128 cohorts, uint128 mp, uint128 vu, uint128 bond, uint128 pool,,) = v.bank();
        uint256 buckets = uint256(cohorts) + mp + vu + bond + pool;
        assertLe(buckets, v.balanceOf(vault), "vault-held shares cover the bank");
        assertEq(v.balanceOf(vault) - buckets, gift, "unsolicited shares are inert");
        assertEq(bond, 1e24 - 1e15, "a share gift does not enter the bond");
    }

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

    /// @notice M-7: the donation (first-depositor) attack, as a grid over the attacker's stake, the donation and the
    ///         victim's size, with the donation made in all three custody forms (ERC-20 tokens, ERC-6909 claims and
    ///         vault shares). The victim's cost and the attacker's payout equal those of the same run without the
    ///         donation, so the attack's P&L is exactly minus the donation.
    function test_M_7__donationAttackLosesMoney(uint256 stake, uint256 donation, uint256 victim) public {
        stake = bound(stake, 1, 1e24);
        uint256 d = bound(donation, 1, 1e21);
        victim = bound(victim, 1, 1e25);
        address attacker = makeAddr("attacker");
        _fundUser(attacker, 1e25);
        _fundUser(alice, 1e25);
        ClaimDonor donor = new ClaimDonor(manager);
        token0.mint(address(donor), d);

        uint256 snap = vm.snapshotState();
        (uint256 cost0, uint256 cost1, uint256 pay0, uint256 pay1) = _attackRun(attacker, stake, victim, 0, donor);
        vm.revertToState(snap);
        uint256 start = token0.balanceOf(attacker) + token1.balanceOf(attacker);
        (uint256 c0, uint256 c1, uint256 p0, uint256 p1) = _attackRun(attacker, stake, victim, d, donor);
        assertEq(c0, cost0, "victim cost token0 unchanged by the donation");
        assertEq(c1, cost1, "victim cost token1 unchanged by the donation");
        assertEq(p0, pay0, "attacker payout token0 unchanged by the donation");
        assertEq(p1, pay1, "attacker payout token1 unchanged by the donation");
        assertLt(token0.balanceOf(attacker) + token1.balanceOf(attacker), start, "the attack loses money");
    }

    /// @dev attacker deposits `stake`, donates `d` of token0 and token1 and `stake / 2` shares (when `d > 0`), the
    ///      victim deposits `victim` shares, the attacker withdraws `stake - stake / 2` (the same in both runs).
    function _attackRun(address attacker, uint256 stake, uint256 victim, uint256 d, ClaimDonor donor)
        internal
        returns (uint256 c0, uint256 c1, uint256 p0, uint256 p1)
    {
        _deposit(attacker, stake);
        if (d > 0) {
            vm.startPrank(attacker);
            token1.transfer(vault, d); // ERC-20
            v.transfer(vault, stake / 2); // shares
            vm.stopPrank();
            donor.donate(token0, vault, d); // ERC-6909 claims
        }
        (c0, c1) = _deposit(alice, victim);
        (p0, p1) = _withdraw(attacker, stake - stake / 2);
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
