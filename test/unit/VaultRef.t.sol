// SPDX-License-Identifier: MIT
pragma solidity 0.8.26;

import {Vm} from "forge-std/Vm.sol";
import {StateLibrary} from "@uniswap/v4-core/src/libraries/StateLibrary.sol";
import {TickMath} from "@uniswap/v4-core/src/libraries/TickMath.sol";
import {FullMath} from "@uniswap/v4-core/src/libraries/FullMath.sol";
import {LiquidPoolBase} from "../utils/LiquidPoolBase.sol";
import {VaultBase} from "../utils/VaultBase.sol";
import {MockRefPool} from "../utils/MockRefPool.sol";
import {HookMining} from "../../script/Mine.s.sol";
import {ITouchstoneHook} from "../../src/interfaces/ITouchstoneHook.sol";
import {ExecLedger} from "../../src/libraries/ExecLedger.sol";
import {Holdings} from "../../src/types/Holdings.sol";
import {PreSwap} from "../../src/types/PreSwap.sol";

/// @notice The reference price on the vault (spec §3.3, §3.4, §4.6): actions read the live reference and revert when
///         it is unreadable; every other flow is fail-soft (base deferred into `baseOwed`); the charge's reference leg
///         is the live TWAP, which an in-transaction push of the reference pool's spot does not move; a gain at one
///         action never offsets a later action's charge. On the factory-deployed harness over the scripted reference,
///         placed on [-1800, 200) around spot 0.
contract VaultRefTest is LiquidPoolBase {
    using StateLibrary for *;

    uint256 internal constant WAD = 1e18;
    int24 internal constant LO = -1800;
    int24 internal constant HI = 200;
    bytes32 internal constant T_ACTION = keccak256("Action(uint8,uint256,uint160,uint256,uint256)");
    bytes32 internal constant T_DEFERRED = keccak256("CheckpointDeferred()");

    function setUp() public override {
        super.setUp();
        hv.placeForTest(LO, HI);
        _setRef(0);
    }

    // ------------------------------------------------------------------ helpers

    function _setRef(int24 t) internal {
        uint32[] memory s = new uint32[](1);
        int24[] memory k = new int24[](1);
        (s[0], k[0]) = (1, t);
        refPool.setPath(s, k);
    }

    function _sqrt(int24 t) internal pure returns (uint160) {
        return TickMath.getSqrtPriceAtTick(t);
    }

    function _sqrtSpot() internal view returns (uint160 sp) {
        (sp,,,) = manager.getSlot0(poolId);
    }

    function _modes() internal pure returns (MockRefPool.Mode[5] memory) {
        return [
            MockRefPool.Mode.Old,
            MockRefPool.Mode.Revert,
            MockRefPool.Mode.Short,
            MockRefPool.Mode.GasBomb,
            MockRefPool.Mode.ReturnBomb
        ];
    }

    function _swapCall() internal {
        vm.prank(curator);
        hv.rebalanceSwap(true, 1e16, type(uint256).max);
    }

    function _placeCall() internal {
        vm.prank(curator);
        hv.setPositions(LO, HI, PreSwap(false, false, 0), 0, type(uint256).max);
    }

    /// @notice The `Action` event's charge and price in the recorded logs (reverts if none).
    function _action(Vm.Log[] memory logs) internal view returns (uint256 charge, uint160 pStar) {
        for (uint256 i; i < logs.length; ++i) {
            if (logs[i].emitter == address(hv) && logs[i].topics[0] == T_ACTION) {
                (, charge, pStar,,) = abi.decode(logs[i].data, (uint8, uint256, uint160, uint256, uint256));
                return (charge, pStar);
            }
        }
        revert("no Action event");
    }

    /// @notice One swap action by the curator: its holdings before (after the prologue), after, the spot before, the
    ///         charge and the price of the worse leg from its `Action` event.
    function _swapMeasured(bool z, uint128 amt)
        internal
        returns (Holdings memory b, Holdings memory a, uint160 s0, uint256 charge, uint160 pStar)
    {
        hv.settle(); // the prologue in this block: the holdings read next are the action's `before`
        b = hv.holdings();
        s0 = _sqrtSpot();
        vm.recordLogs();
        vm.prank(curator);
        hv.rebalanceSwap(z, amt, type(uint256).max);
        (charge, pStar) = _action(vm.getRecordedLogs());
        a = hv.holdings();
    }

    function _measure(Holdings memory b, Holdings memory a, uint160 s0, int24 ref)
        internal
        view
        returns (uint256 charge, uint160 pStar)
    {
        (uint256 navSpot,) = ExecLedger.value(b, s0);
        uint256 reserve = FullMath.mulDiv(tier.RESERVE_RATIO, navSpot, WAD);
        (charge, pStar,,,,) = ExecLedger.measure(b, a, s0, s0, _sqrt(ref), reserve, hv.totalSupply(), 0, 0);
    }

    // ------------------------------------------------------------------ U-ref

    /// @notice Every unreadable mode of the reference reverts both curator actions with `RefUnreadable` (the cache is
    ///         fresh, so only the live read fails). Control: the same actions pass with the reference readable.
    function test_U_ref__unreadableReferenceRevertsBothActions() public {
        advance(tier.COOLDOWN);
        ITouchstoneHook(hook).refreshRef();
        MockRefPool.Mode[5] memory modes = _modes();
        for (uint256 i; i < modes.length; ++i) {
            refPool.setMode(modes[i]);
            assertTrue(_obs().refFresh, "the cache is fresh");
            vm.prank(curator);
            vm.expectRevert(ITouchstoneHook.RefUnreadable.selector);
            hv.rebalanceSwap(true, 1e16, type(uint256).max);
            vm.prank(curator);
            vm.expectRevert(ITouchstoneHook.RefUnreadable.selector);
            hv.setPositions(LO, HI, PreSwap(false, false, 0), 0, type(uint256).max);
        }
        refPool.setMode(MockRefPool.Mode.Ok);
        uint256 snap = vm.snapshotState();
        _swapCall(); // control
        vm.revertToState(snap);
        _placeCall(); // control
    }

    /// @notice A push of the reference pool's spot inside the action's own transaction does not move the TWAP the
    ///         action reads (the window ends at the block's start): an action right after a push of the reference to
    ///         +5000 passes the fence and is charged at the old reference. Control: one block later the TWAP has
    ///         moved and the same action reverts `Fence`.
    function test_U_ref__inTransactionPushOfTheReferenceDoesNotMoveTheTwap() public {
        uint32[] memory s = new uint32[](2);
        int24[] memory k = new int24[](2);
        (s[0], k[0]) = (1, 0);
        (s[1], k[1]) = (uint32(vm.getBlockTimestamp()), 5000); // the reference's spot pushed in this block
        refPool.setPath(s, k);
        (,int24 refSpot,,,,,) = refPool.slot0();
        assertEq(refSpot, 5000, "the reference's spot moved");
        assertEq(ITouchstoneHook(hook).liveRef(), 0, "its TWAP did not");
        (Holdings memory b, Holdings memory a, uint160 s0, uint256 charge, uint160 pStar) = _swapMeasured(true, 1e16);
        (uint256 c, uint160 p) = _measure(b, a, s0, 0);
        assertEq(charge, c, "charged at the TWAP");
        assertEq(pStar, p);
        advance(tier.COOLDOWN);
        assertTrue(ITouchstoneHook(hook).liveRef() != 0, "control: the TWAP moves once time passes");
        vm.prank(curator);
        vm.expectRevert();
        hv.rebalanceSwap(true, 1e16, type(uint256).max);
    }

    /// @notice The charge's reference leg is the live reference, not the hook's cache: with the cache at 0 and the live
    ///         TWAP at 40, a swap that sells token0 is charged at the 40 leg (its worse leg) exactly; control: the
    ///         measure at the cache's 0 gives a different charge.
    function test_U_ref__chargeReferenceLegIsTheLiveReferenceNotTheCache() public {
        ITouchstoneHook(hook).refreshRef();
        _setRef(40);
        assertEq(_obs().refTick, 0, "the cache holds 0");
        assertEq(ITouchstoneHook(hook).liveRef(), 40, "the live reference is 40");
        (Holdings memory b, Holdings memory a, uint160 s0, uint256 charge, uint160 pStar) = _swapMeasured(true, 2e16);
        (uint256 cLive, uint160 pLive) = _measure(b, a, s0, 40);
        (uint256 cCache,) = _measure(b, a, s0, 0);
        assertEq(pStar, _sqrt(40), "the worse leg is the live reference");
        assertEq(pLive, pStar);
        assertEq(charge, cLive, "charged at the live reference");
        assertGt(charge, cCache, "control: the cache's leg would charge less");
    }

    // ------------------------------------------------------------------ U-ref-vault

    /// @notice With the reference unreadable (every mode) and the cache gone stale, deposits, withdrawals and
    ///         checkpoints succeed, the base earned while the clock was observed is deferred into `baseOwed` (with
    ///         `CheckpointDeferred`), and both curator actions revert `RefUnreadable`. Control: once the reference reads
    ///         again, a checkpoint mints the owed base and `baseOwed` returns to 0.
    function test_U_ref_vault__flowsAndCheckpointsSucceedWhileActionsRevert() public {
        assertGt(tier.m_perBlock, 0, "the tier's real m");
        // a real curator placement (the test seam leaves delta0 at 0), its markout settled
        advance(tier.COOLDOWN);
        _placeCall();
        _keepAlive(tier.H_MARKOUT + 20);
        hv.settle();
        assertGt(hv.scalars().delta0, 0, "delta0 set by the placement");
        MockRefPool.Mode[5] memory modes = _modes();
        for (uint256 i; i < modes.length; ++i) {
            uint256 snap = vm.snapshotState();
            _keepAlive(40); // observed in-range time: tau_v grows, no vault checkpoint
            refPool.setMode(modes[i]);
            for (uint256 j; j < 20 && _obs().refFresh; ++j) {
                advance(tier.GRID);
                ITouchstoneHook(hook).refreshRef();
            }
            assertFalse(_obs().refFresh, "the cache is stale");
            uint256 S = hv.totalSupply();
            vm.recordLogs();
            _deposit(bob, 1e21);
            Vm.Log[] memory logs = vm.getRecordedLogs();
            bool deferred;
            for (uint256 j; j < logs.length; ++j) {
                if (logs[j].emitter == address(hv) && logs[j].topics[0] == T_DEFERRED) deferred = true;
            }
            assertTrue(deferred, "CheckpointDeferred");
            uint256 owed = hv.scalars().baseOwed;
            assertGt(owed, 0, "base deferred into baseOwed");
            assertEq(hv.totalSupply(), S + 1e21, "no base shares minted, the deposit's shares only");
            _withdraw(bob, 5e20);
            advance(1);
            hv.settle(); // a checkpoint
            assertGe(hv.scalars().baseOwed, owed, "still owed");
            vm.prank(curator);
            vm.expectRevert(ITouchstoneHook.RefUnreadable.selector);
            hv.rebalanceSwap(true, 1e16, type(uint256).max);
            vm.prank(curator);
            vm.expectRevert(ITouchstoneHook.RefUnreadable.selector);
            hv.setPositions(LO, HI, PreSwap(false, false, 0), 0, type(uint256).max);
            // control: readable again, the next checkpoint mints the owed base
            refPool.setMode(MockRefPool.Mode.Ok);
            advance(1);
            uint256 S1 = hv.totalSupply();
            hv.settle();
            assertEq(hv.scalars().baseOwed, 0, "control: owed base minted");
            assertGt(hv.totalSupply(), S1, "control: shares minted");
            vm.revertToState(snap);
        }
    }

    // ------------------------------------------------------------------ U-net

    /// @notice A gain never offsets a charge (`max(0, ·)` per action): action A sells token0 with the reference 40
    ///         ticks below spot, a gain at the reference leg, and is charged its spot-leg loss in full; action B, with
    ///         the reference 40 ticks above, is charged exactly its own measure, with nothing carried from A's gain.
    function test_U_net__aGainNeverOffsetsAChargeOrALaterCharge() public {
        _setRef(-40);
        (Holdings memory bA, Holdings memory aA, uint160 s0A, uint256 cA,) = _swapMeasured(true, 2e16);
        (uint256 nbRef,) = ExecLedger.value(bA, _sqrt(-40));
        (uint256 naRef,) = ExecLedger.value(aA, _sqrt(-40));
        assertGt(naRef, nbRef, "A gains at the reference leg");
        (uint256 expA,) = _measure(bA, aA, s0A, -40);
        assertEq(cA, expA, "A is charged its worse leg");
        assertGt(cA, 0, "the reference leg's gain does not net A's spot-leg loss");
        advance(tier.COOLDOWN);
        _setRef(40);
        (Holdings memory bB, Holdings memory aB, uint160 s0B, uint256 cB,) = _swapMeasured(true, 2e16);
        (uint256 expB,) = _measure(bB, aB, s0B, 40);
        assertEq(cB, expB, "B is charged its own measure");
        uint256 gainA = naRef - nbRef;
        assertGt(cB, 0);
        assertTrue(cB != (cB > gainA ? cB - gainA : 0), "control: a netted charge would differ");
    }

    /// @notice `measure`'s floor: holdings that gain at every leg are charged 0 and burn nothing (never a credit).
    function test_U_net__gainAtEveryLegIsChargedZero(uint96 i0, uint96 i1, uint64 g0, uint64 g1, int24 t0, int24 t1)
        public
        pure
    {
        t0 = int24(bound(t0, -50_000, 50_000));
        t1 = int24(bound(t1, -50_000, 50_000));
        Holdings memory b = Holdings(i0, i1, 0, 0, 0);
        Holdings memory a = Holdings(uint128(i0) + g0, uint128(i1) + g1, 0, 0, 0);
        (uint256 charge,,, uint256 burn,,) =
            ExecLedger.measure(b, a, _sqrt(t0), _sqrt(t1), _sqrt(t0 / 2), 0, 1e24, 0, 0);
        assertEq(charge, 0);
        assertEq(burn, 0);
    }
}

/// @notice U-ref-seed (spec §3.3 armAtSeed, §4.6 seed): the seed requires a successful reference refresh. The production
///         vault and extension through the factory; the reference's read is starved below its gas floor only on the
///         refresh path (the hook's constructor reads it with all the gas, the refresh with `REF_GAS_CAP`).
contract VaultRefSeedTest is VaultBase {
    function setUp() public override {
        _setUpEnv();
    }

    function _deployReverts(bytes4 sel) internal {
        address pv = HookMining.vaultAddress(address(factory), deployer, SALT_V, _vaultCode());
        hookArgs = _hookArgsFor(pv);
        (saltH,) = HookMining.mine(address(factory), deployer, _hookCode(), hookArgs, 0);
        _fund(deployer);
        vm.expectRevert(sel);
        factory.deploy(_vaultCode(), _extCode(), _hookCode(), hookArgs, SALT_V, saltH, seedParams);
    }

    /// @notice A reference whose read fails at the seed's refresh fails the whole deployment with `RefUnreadable`;
    ///         control: the same deployment succeeds with the reference readable, and the cache is written in the
    ///         seed's block.
    function test_U_ref_seed__seedRequiresASuccessfulRefresh() public {
        refPool.setGasFloor(tier.REF_GAS_CAP + 1);
        _deployReverts(ITouchstoneHook.RefUnreadable.selector);
        refPool.setGasFloor(0);
        (vault, hook) = _deploy(deployer, SALT_V);
        assertEq(ITouchstoneHook(hook).obs().refCachedAt, vm.getBlockNumber(), "control: cached at seed");
    }
}

/// @notice U-ref-seed, second clause: a placement in the seed's grid (the cache from `armAtSeed`, the live reference
///         since moved) re-bases `NAV₀` at the live reference, exactly `NAV_after(s_ref)`.
contract VaultRefGridZeroTest is LiquidPoolBase {
    function _setRef(int24 t) internal {
        uint32[] memory s = new uint32[](1);
        int24[] memory k = new int24[](1);
        (s[0], k[0]) = (1, t);
        refPool.setPath(s, k);
    }

    function test_U_ref_seed__placementInGridZeroRebasesNavAtTheLiveReference() public {
        uint32 g = _obs().gridIndex;
        _setRef(30); // the live TWAP moves; the cache keeps the seed's anchor
        assertEq(_obs().refTick, 0, "the cache holds the seed's reference");
        vm.prank(curator);
        hv.setPositions(-1800, 200, PreSwap(false, false, 0), 0, type(uint256).max);
        assertEq(_obs().gridIndex, g, "still in the seed's grid");
        Holdings memory h = hv.holdings();
        (uint256 navLive,) = ExecLedger.value(h, TickMath.getSqrtPriceAtTick(30));
        (uint256 navCache,) = ExecLedger.value(h, TickMath.getSqrtPriceAtTick(0));
        assertEq(hv.scalars().nav0, navLive, "NAV0 = NAV_after(live reference)");
        assertTrue(navLive != navCache, "control: the cache's price gives another NAV0");
    }
}
