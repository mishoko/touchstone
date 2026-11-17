// SPDX-License-Identifier: MIT
pragma solidity 0.8.26;

import {Vm} from "forge-std/Vm.sol";
import {StateLibrary} from "@uniswap/v4-core/src/libraries/StateLibrary.sol";
import {TickMath} from "@uniswap/v4-core/src/libraries/TickMath.sol";
import {FullMath} from "@uniswap/v4-core/src/libraries/FullMath.sol";
import {ModifyLiquidityParams} from "@uniswap/v4-core/src/types/PoolOperation.sol";
import {LiquidPoolBase} from "../utils/LiquidPoolBase.sol";
import {MockRefPool} from "../utils/MockRefPool.sol";
import {ExpectedFlows} from "../utils/ExpectedFlows.sol";
import {Scalars} from "../utils/VaultHarness.sol";
import {TouchstoneVault} from "../../src/TouchstoneVault.sol";
import {ITouchstoneHook} from "../../src/interfaces/ITouchstoneHook.sol";
import {ExecLedger} from "../../src/libraries/ExecLedger.sol";
import {FeeBank} from "../../src/libraries/FeeBank.sol";
import {Ladder} from "../../src/libraries/Ladder.sol";
import {Markout} from "../../src/types/Markout.sol";
import {Holdings} from "../../src/types/Holdings.sol";
import {Bank, Cohort, RING} from "../../src/types/Bank.sol";
import {SettleExtraOracle} from "../utils/SettleExtraOracle.sol";
import {IVaultEntries} from "../../src/interfaces/ITouchstoneVaultExtension.sol";
import {VaultCommon} from "../../src/TouchstoneVaultBase.sol";

/// @notice The curator's `rebalanceSwap` and the charge path it shares with `setPositions`, on the real hook over the
///         scripted reference pool. The vault holds a position on [-1800, 200) around spot 0 (so most of its token0
///         stays idle and can be swapped). Expectations come from `ExecLedger.measure`/`value`/`settleExtra` (checked
///         bit for bit against the reference model's vectors) on inputs the test reads itself: the holdings before
///         the action (after collect-first), the pool's spot, the scripted reference tick and the supply.
contract VaultActionTest is LiquidPoolBase, SettleExtraOracle {
    using StateLibrary for *;

    event Action(uint8 kind, uint256 charge, uint160 pStar, uint256 burnedBank, uint256 burnedBond);
    event Settled(uint256 charge0, uint256 extra, int24 tickMk, bool fallbackUsed);

    uint256 internal constant WAD = 1e18;
    uint8 internal constant KIND_SWAP = 3; // uint8(Entry.RebalanceSwap)
    int24 internal constant LO = -1800;
    int24 internal constant HI = 200;

    struct Exp {
        uint256 charge;
        uint160 pStar;
        uint256 burn;
        uint256 burnR;
        uint256 reserve;
    }

    function setUp() public override {
        super.setUp();
        hv.placeForTest(LO, HI);
        _setRef(0);
    }

    // ------------------------------------------------------------------ helpers

    /// @notice The reference holds `t` over its whole history (its live TWAP is `t` at once; the hook's cache keeps
    ///         its last anchor until the next one).
    function _setRef(int24 t) internal {
        uint32[] memory s = new uint32[](1);
        int24[] memory k = new int24[](1);
        (s[0], k[0]) = (1, t);
        refPool.setPath(s, k);
    }

    function _sqrtSpot() internal view returns (uint160 sp) {
        (sp,,,) = manager.getSlot0(poolId);
    }

    function _swapAs(bool z, uint128 amt, uint256 maxC) internal {
        vm.prank(curator);
        hv.rebalanceSwap(z, amt, maxC);
    }

    /// @notice Runs the prologue in this block (collect-first and the checkpoint), so the holdings read next are the
    ///         action's `before`.
    function _prime() internal returns (Holdings memory before, uint256 S) {
        hv.settle();
        return (hv.holdings(), hv.totalSupply());
    }

    function _expect(Holdings memory b, Holdings memory a, uint160 s0, int24 ref, uint256 S)
        internal
        view
        returns (Exp memory e)
    {
        (uint256 navSpot,) = ExecLedger.value(b, s0);
        e.reserve = FullMath.mulDiv(tier.RESERVE_RATIO, navSpot, WAD);
        (e.charge, e.pStar,, e.burn, e.burnR,) =
            ExecLedger.measure(b, a, s0, s0, TickMath.getSqrtPriceAtTick(ref), e.reserve, S, 0, 0);
    }

    /// @notice The fraction `ceil(value_ref(amountIn)·WAD / NAV_ref(holdings))` the swap caps compare.
    function _frac(Holdings memory h, bool z, uint256 amt, int24 ref) internal view returns (uint256) {
        uint160 sr = TickMath.getSqrtPriceAtTick(ref);
        (uint256 nav,) = ExecLedger.value(h, sr);
        (uint256 v,) = ExecLedger.value(Holdings(z ? uint128(amt) : 0, z ? 0 : uint128(amt), 0, 0, 0), sr);
        return FullMath.mulDivRoundingUp(v, WAD, nav);
    }

    /// @notice The largest token0 amount whose fraction is at most `capFrac`.
    function _capAmount0(Holdings memory h, uint256 capFrac, int24 ref) internal view returns (uint256 a) {
        uint160 sr = TickMath.getSqrtPriceAtTick(ref);
        (uint256 nav,) = ExecLedger.value(h, sr);
        uint256 lim = FullMath.mulDiv(capFrac, nav, WAD); // the largest value with value·WAD ≤ capFrac·nav
        // q(a, sr) is monotone in a: binary search the largest a with q(a) ≤ lim
        uint256 hiA = lim * 2 + 2;
        while (a < hiA) {
            uint256 mid = (a + hiA + 1) / 2;
            (uint256 v,) = ExecLedger.value(Holdings(uint128(mid), 0, 0, 0, 0), sr);
            if (v <= lim) a = mid;
            else hiA = mid - 1;
        }
    }

    function _scalars() internal view returns (Scalars memory) {
        return hv.scalars();
    }

    function _setCounters(uint64 frac, uint16 count, uint128 nav0, uint64 delta0) internal {
        Scalars memory s = hv.scalars();
        hv.setScalars(nav0, delta0, s.tauV0, s.segStart, s.baseOwed, s.epoch, frac, count);
    }

    function _removeThirdPartyLiquidity() internal {
        lpRouter.modifyLiquidity(key, ModifyLiquidityParams(-6000, 6000, -int256(_thirdPartyL()), 0), "");
    }

    // ------------------------------------------------------------------ U-3: the charge through the vault

    /// @notice U-settle (s15, F-044): an action's record keeps what the fee-aware settlement needs: the pre-move spot
    ///         `s0`, the spot the action leaves `sEnd`, the charge's burn `burn0` (the supply's drop in the action) and the
    ///         supply just before it `sAct`.
    function test_U_settle__recordKeepsTheSpotsTheBurnAndTheSupply() public {
        _setRef(40); // the swap sells token0 below the reference: a positive charge
        (, uint256 S0) = _prime();
        uint160 spBefore = _sqrtSpot();
        _swapAs(true, 2e16, type(uint256).max);
        Markout memory m = hv.markout();
        uint160 spAfter = _sqrtSpot();
        uint256 S1 = hv.totalSupply();
        assertEq(m.s0, spBefore, "s0: the pre-move spot");
        assertEq(m.sEnd, spAfter, "sEnd: the spot the action left");
        assertTrue(m.s0 != m.sEnd, "the swap moved spot");
        assertGt(m.burn0, 0, "the charge burned shares");
        assertEq(m.sAct, S0, "sAct: the supply before the charge's burn");
        assertEq(uint256(m.burn0), S0 - S1, "burn0: the supply's drop in the action");
    }

    /// @notice A swap is charged its shortfall at the worse leg: the burn leaves the supply, the reserve's shares are
    ///         encumbered out of the bond, the pending record holds the holdings before and after, the charge and the
    ///         reserve, and the `Action` event carries the charge, its leg and the bank and bond burns.
    function test_U_3__swapChargedItsShortfallAtTheWorseLeg() public {
        _setRef(40); // spot 0 sells token0 below the reference: the reference leg is the worse one
        (Holdings memory b, uint256 S) = _prime();
        uint160 s0 = _sqrtSpot();
        Bank memory bk0 = hv.bankFull();
        vm.recordLogs();
        _swapAs(true, 2e16, type(uint256).max);
        Holdings memory a = hv.holdings();
        Exp memory e = _expect(b, a, s0, 40, S);
        assertGt(e.charge, 0, "a positive charge");
        assertEq(e.pStar, TickMath.getSqrtPriceAtTick(40), "the reference leg");
        assertLt(a.idle0, b.idle0, "token0 left idle");
        assertGt(a.idle1, b.idle1, "token1 entered idle");
        // the event
        (uint256 c, uint160 p, uint256 bb, uint256 bd) = _actionLog();
        assertEq(c, e.charge, "event charge");
        assertEq(p, e.pStar, "event pStar");
        assertEq(bb, 0, "no unvested pay: nothing from the bank");
        assertEq(bd, e.burn, "the burn from the bond");
        // the burn and the reserve
        assertEq(hv.totalSupply(), S - e.burn, "the supply loses exactly the burn");
        Bank memory bk = hv.bankFull();
        assertEq(bk.reserveShares, e.burnR, "the reserve's shares");
        assertEq(bk.reserveBond, e.burnR, "all from the bond");
        assertEq(bk.bondShares, bk0.bondShares - e.burn - e.burnR, "the bond pays both");
        assertFalse(bk.reserveForfeit, "a fresh reserve");
        // the pending record
        (uint40 tA, Holdings memory pb, Holdings memory pa, uint128 c0, uint128 r) = hv.pendingAction();
        assertEq(tA, vm.getBlockTimestamp(), "tAction");
        _eqH(pb, b, "record before");
        _eqH(pa, a, "record after");
        assertEq(c0, e.charge, "record charge");
        assertEq(r, e.reserve, "record reserve");
        // custody: the claims match the ledger after the swap's settlement through claims
        assertEq(manager.balanceOf(address(hv), token0Id()), a.idle0, "claims0 = idle0");
        assertEq(manager.balanceOf(address(hv), token1Id()), a.idle1, "claims1 = idle1");
    }

    /// @notice Collect after the swap, before `NAV_after`: the vault's own swap pays fees into its own position (the
    ///         only liquidity here); they are swept into idle in the same call and are part of the recorded `after`.
    function test_U_3__collectAfterTheSwapBeforeNavAfter() public {
        _removeThirdPartyLiquidity();
        (Holdings memory b, uint256 S) = _prime();
        (uint256 g0,) = manager.getFeeGrowthInside(poolId, LO, HI);
        uint160 s0 = _sqrtSpot();
        vm.recordLogs();
        _swapAs(true, 2e16, type(uint256).max);
        (uint256 g1,) = manager.getFeeGrowthInside(poolId, LO, HI);
        assertGt(g1, g0, "control: the swap paid fees into the vault's own position");
        // the amount swapped in, from the PoolManager's own Swap event (the swapper's delta: amount0 < 0 paid in)
        uint256 paid0 = _swapEventPaid0();
        (uint256 f0, uint256 f1) = ExpectedFlows.fees(manager, poolId, address(hv), LO, HI);
        assertEq(f0 + f1, 0, "nothing left uncollected");
        Holdings memory a = hv.holdings();
        (,, Holdings memory pa,,) = hv.pendingAction();
        _eqH(pa, a, "the record holds the holdings after");
        // independent: idle0 = before − the amount swapped in (the PoolManager's event) + the token0 fee the collect
        // brought back (the fee growth inside the vault's range times its L)
        uint256 fee0;
        unchecked {
            fee0 = FullMath.mulDiv(g1 - g0, b.L, 1 << 128);
        }
        assertGt(fee0, 0, "a token0 fee");
        assertEq(pa.idle0, b.idle0 - paid0 + fee0, "after includes the collected fee");
        Exp memory e = _expect(b, a, s0, 0, S);
        (,, , uint128 c0,) = hv.pendingAction();
        assertEq(c0, e.charge, "charged on the collected holdings");
    }

    /// @notice The charge measures the holdings at the action, not those recorded at the last one: a deposit between
    ///         two actions changes the holdings and the second action is charged on the new ones.
    function test_U_3__chargeMeasuresTheHoldingsAtTheAction() public {
        _swapAs(true, 1e16, type(uint256).max);
        advance(tier.COOLDOWN);
        hv.settle(); // the markout is due: settled
        _deposit(bob, 5e22);
        advance(1);
        (Holdings memory b, uint256 S) = _prime();
        uint160 s0 = _sqrtSpot();
        _swapAs(false, 1e16, type(uint256).max);
        (, Holdings memory pb, Holdings memory pa, uint128 c0, uint128 r) = hv.pendingAction();
        _eqH(pb, b, "before = the holdings at this action");
        Exp memory e = _expect(b, pa, s0, 0, S);
        assertEq(c0, e.charge, "charge");
        assertEq(r, e.reserve, "reserve");
    }

    // ------------------------------------------------------------------ maxCharge (U-settle, action side)

    /// @notice `maxCharge` compares `charge₀ + reserve`: exactly that passes, one less reverts `MaxCharge`.
    function test_U_settle__maxChargeComparesChargePlusReserve() public {
        _setRef(40);
        uint256 snap = vm.snapshotState();
        _swapAs(true, 2e16, type(uint256).max);
        (,,, uint128 c0, uint128 r) = hv.pendingAction();
        assertGt(c0, 0, "a charge");
        assertGt(r, 0, "a reserve");
        vm.revertToState(snap);
        snap = vm.snapshotState();
        vm.prank(curator);
        vm.expectRevert(VaultCommon.MaxCharge.selector);
        hv.rebalanceSwap(true, 2e16, uint256(c0) + r - 1);
        // charge₀ alone is below the backstop: a check on charge₀ only would pass here
        assertLt(uint256(c0), uint256(c0) + r - 1, "the reserve matters");
        vm.revertToState(snap);
        _swapAs(true, 2e16, uint256(c0) + r);
        (uint40 tA,,,,) = hv.pendingAction();
        assertEq(tA, vm.getBlockTimestamp(), "acted at exactly charge + reserve");
    }

    /// @notice A real action's markout settles at its window on the recorded holdings (prologue step 0 of `settle()`).
    function test_U_settle__realActionSettlesAtItsWindow() public {
        _swapAs(true, 2e16, type(uint256).max);
        uint256 tA = vm.getBlockTimestamp();
        (, Holdings memory pb, Holdings memory pa, uint128 c0, uint128 r) = hv.pendingAction();
        uint128 rs = hv.bankFull().reserveShares;
        // deposits wait for the settlement
        vm.prank(bob);
        vm.expectRevert(VaultCommon.MarkoutPending.selector);
        IVaultEntries(address(hv)).deposit(1e18, type(uint256).max, type(uint256).max, bob);
        advance(1);
        refPool.setTick(-30); // the window's tick: the sold token0 cheaper than at the action
        advance(tier.H_MARKOUT / blocktime);
        refPool.setTick(0);
        advance(1);
        assertEq(vm.getBlockTimestamp(), tA + 2 * blocktime + tier.H_MARKOUT, "due");
        (uint256 extra,, uint256 burnS) =
            _settleExtra(pb, pa, hv.markout(), -30, tier.FEE_TICKS, hv.feePips(), rs);
        uint256 S = hv.totalSupply();
        uint256 banked = _cohorts();
        vm.expectEmit(address(hv));
        emit Settled(c0, extra, -30, false);
        hv.settle();
        // the settlement burn, plus the base the same call's checkpoint mints after it (step 0 runs first)
        assertEq(hv.totalSupply(), S - burnS + (_cohorts() - banked), "the settlement burn");
        assertEq(hv.bankFull().reserveShares, 0, "reserve released");
        _deposit(bob, 1e18); // and deposits reopen
    }

    /// @notice S15D-07 (spec §3.4): base minted by a checkpoint inside a pending action's markout window raises the
    ///         record's `sAct` by exactly the shares minted, and the settlement then sizes its burn on the raised supply.
    ///         Control: the oracle on the un-raised record gives a different burn.
    function test_U_settle__baseMintedInsideTheWindowRaisesSAct() public {
        _swapAs(true, 2e16, type(uint256).max);
        uint256 tA = vm.getBlockTimestamp();
        (, Holdings memory pb, Holdings memory pa,,) = hv.pendingAction();
        uint128 rs = hv.bankFull().reserveShares;
        Markout memory m0 = hv.markout();
        advance(1);
        refPool.setTick(100); // adverse: the sold token0 dearer than at the action
        advance(3);
        // a checkpoint inside the window (not due: no settlement), which mints base
        uint256 S0 = hv.totalSupply();
        hv.settle();
        uint256 minted = hv.totalSupply() - S0;
        assertGt(minted, 0, "base minted inside the window");
        assertEq(hv.markout().sAct, m0.sAct + minted, "sAct raised by the shares minted");
        assertTrue(hv.markout().sAct > m0.sAct, "raised");
        advance(tier.H_MARKOUT / blocktime - 3);
        refPool.setTick(0);
        advance(1);
        assertEq(vm.getBlockTimestamp(), tA + 2 * blocktime + tier.H_MARKOUT, "due");
        (,, uint256 burnS) = _settleExtra(pb, pa, hv.markout(), 100, tier.FEE_TICKS, hv.feePips(), rs);
        (,, uint256 burnUnraised) = _settleExtra(pb, pa, m0, 100, tier.FEE_TICKS, hv.feePips(), rs);
        assertGt(burnS, 0, "a burn");
        assertTrue(burnS != burnUnraised, "control: the raise changes the burn");
        uint256 S = hv.totalSupply();
        uint256 banked = _cohorts();
        hv.settle();
        assertEq(hv.totalSupply(), S - burnS + (_cohorts() - banked), "the burn on the raised sAct");
    }

    function _cohorts() internal view returns (uint256 c) {
        Bank memory b = hv.bankFull();
        for (uint256 k; k < RING; ++k) {
            c += b.cohorts[k].shares;
        }
    }

    /// @notice A second action settles the first one's due markout first (the cooldown outlasts the window), then
    ///         records its own reserve.
    function test_U_settle__nextActionSettlesThePendingOneFirst() public {
        _swapAs(true, 1e16, type(uint256).max);
        advance(tier.COOLDOWN);
        vm.recordLogs();
        _swapAs(true, 1e16, type(uint256).max);
        Vm.Log[] memory logs = vm.getRecordedLogs();
        bool settled;
        bytes32 sig = keccak256("Settled(uint256,uint256,int24,bool)");
        for (uint256 i; i < logs.length; ++i) {
            if (logs[i].emitter == address(hv) && logs[i].topics[0] == sig) settled = true;
        }
        assertTrue(settled, "the pending action settled first");
        (uint40 tA,,,,) = hv.pendingAction();
        assertEq(tA, vm.getBlockTimestamp(), "the new action is pending");
        assertGt(hv.bankFull().reserveShares, 0, "its reserve");
    }

    // ------------------------------------------------------------------ U-solv

    /// @notice The reserve is `RESERVE_RATIO·NAV_before(s_spot)`, not `NAV_after(s_spot)` nor `NAV_before(s_ref)`
    ///         (spot and the reference apart, and a charge, so the three differ).
    function test_U_solv__reserveAtNavBeforeSpot() public {
        _setRef(50);
        (Holdings memory b,) = _prime();
        uint160 s0 = _sqrtSpot();
        _swapAs(true, 2e16, type(uint256).max);
        (,,, , uint128 r) = hv.pendingAction();
        Holdings memory a = hv.holdings();
        (uint256 nbSpot,) = ExecLedger.value(b, s0);
        (uint256 naSpot,) = ExecLedger.value(a, s0);
        (uint256 nbRef,) = ExecLedger.value(b, TickMath.getSqrtPriceAtTick(50));
        uint256 want = FullMath.mulDiv(tier.RESERVE_RATIO, nbSpot, WAD);
        assertEq(r, want, "NAV_before(s_spot)");
        assertNotEq(want, FullMath.mulDiv(tier.RESERVE_RATIO, naSpot, WAD), "control: NAV_after differs");
        assertNotEq(want, FullMath.mulDiv(tier.RESERVE_RATIO, nbRef, WAD), "control: s_ref differs");
    }

    /// @notice The worst swap (the per-action cap, spot at the fence below the reference, sold at the adverse limit's
    ///         side) plus the reserve fits half the deployed bond ratio's cover (the 2× rule), and a bond at exactly
    ///         the burns passes the gate; one share less reverts `Insolvent` (control).
    function test_U_solv__worstSwapPlusReserveFitsTheBond() public {
        _setRef(tier.REF_BAND); // spot 0 at the fence below the reference
        // JIT at the limit: the third-party depth is replaced by a position on [-10, 0), just above the adverse limit,
        // sized so that the per-action amount fills (almost) completely with the price reaching the limit
        _removeThirdPartyLiquidity();
        lpRouter.modifyLiquidity(key, ModifyLiquidityParams(-10, 0, 8e19, 0), "");
        (Holdings memory b, uint256 S) = _prime();
        uint160 s0 = _sqrtSpot();
        (uint256 g0,) = manager.getFeeGrowthInside(poolId, LO, HI);
        uint256 amt = _capAmount0(b, tier.MAX_SWAP_ACTION, tier.REF_BAND);
        uint256 snap = vm.snapshotState();
        _swapAs(true, uint128(amt), type(uint256).max);
        (, Holdings memory pb, Holdings memory pa, uint128 c0, uint128 r) = hv.pendingAction();
        _eqH(pb, b, "before");
        Exp memory e = _expect(b, pa, s0, tier.REF_BAND, S);
        (,, uint256 navBurn,,,) = ExecLedger.measure(
            pb, pa, s0, s0, TickMath.getSqrtPriceAtTick(tier.REF_BAND), 0, S, 0, 0
        );
        // the binding constraint: the adverse limit reached with the request filled (to 1e-4)
        assertLe(_tick(), -(tier.FEE_TICKS + tier.BAND_SWAP), "the price reached the adverse limit");
        (uint256 g1,) = manager.getFeeGrowthInside(poolId, LO, HI);
        uint256 filled = pb.idle0 - pa.idle0 + FullMath.mulDiv(g1 - g0, pb.L, 1 << 128);
        assertGe(filled * 1e4, amt * (1e4 - 1), "the request filled");
        // the 2× rule on the gate's own quantity: (charge₀ + reserve)·2 ≤ BOND_RATIO·NAV′
        assertLe((uint256(c0) + r) * 2 * WAD, uint256(tier.BOND_RATIO) * navBurn, "2x cover at BOND_RATIO");
        // control: the worst swap of the bond sizing (fee + FEE_TICKS + BAND_SWAP + REF_BAND: 1.38e-4 of NAV′)
        assertGe(uint256(c0) * 1e6, navBurn * 137, "charge >= 1.37e-4 of NAV': the worst swap");
        uint256 need = e.burn + e.burnR;
        vm.revertToState(snap);
        // the bond at exactly the burns: passes
        snap = vm.snapshotState();
        _setBond(need);
        _swapAs(true, uint128(amt), type(uint256).max);
        vm.revertToState(snap);
        // one share less: Insolvent, and the vault stays withdrawable
        _setBond(need - 1);
        vm.prank(curator);
        vm.expectRevert(FeeBank.Insolvent.selector);
        hv.rebalanceSwap(true, uint128(amt), type(uint256).max);
        (uint256 o0, uint256 o1) = _withdraw(alice, 1e22);
        assertGt(o0 + o1, 0, "withdrawable");
    }

    // ------------------------------------------------------------------ U-epilogue

    /// @notice After `rebalanceSwap`, `NAV₀` and `δ₀` are re-based at the live reference on the new holdings
    ///         (control: they were set to junk before the action, so a missing re-base is seen).
    function test_U_epilogue__rebasedAtTheLiveReferenceAfterRebalanceSwap() public {
        _setRef(20);
        hv.settle();
        _setCounters(0, 0, 1, 1);
        _swapAs(true, 2e16, type(uint256).max);
        (uint256 nav, uint256 pos) = ExecLedger.value(hv.holdings(), TickMath.getSqrtPriceAtTick(20));
        Scalars memory s = hv.scalars();
        assertEq(s.nav0, nav, "NAV0 at the live reference");
        uint256 d = FullMath.mulDiv(pos, WAD, nav);
        assertEq(s.delta0, d < WAD ? d : WAD, "delta0 at the live reference");
        assertGt(s.delta0, 1, "control: delta0 moved");
    }

    // ------------------------------------------------------------------ U-fence-live

    function _liveObserveCall() internal view returns (bytes memory) {
        uint32[] memory ago = new uint32[](2);
        (ago[0], ago[1]) = (tier.REF_WINDOW, 0);
        return abi.encodeCall(MockRefPool.observe, (ago));
    }

    /// @notice One live read per action: with nothing vestable the action reads the reference itself, once.
    function test_U_fence_live__oneLiveReadPerActionNothingVestable() public {
        ITouchstoneHook(hook).refreshRef(); // this block's snapshot and anchor are done
        vm.expectCall(address(refPool), _liveObserveCall(), 1);
        _swapAs(true, 1e16, type(uint256).max);
    }

    /// @notice One live read per action: with something vestable the checkpoint's successful read is reused.
    function test_U_fence_live__oneLiveReadPerActionReusesTheCheckpointRead() public {
        _seedMatured(1e20);
        ITouchstoneHook(hook).refreshRef();
        vm.expectCall(address(refPool), _liveObserveCall(), 1);
        _swapAs(true, 1e16, type(uint256).max);
        assertEq(hv.bankFull().maturedPending, 0, "control: the gate read and vested");
    }

    /// @notice A failed prologue read is never reused (no tick 0): the action reads again and reverts `RefUnreadable`;
    ///         with nothing vestable the action's own read reverts the same way.
    function test_U_fence_live__failedReadRevertsRefUnreadable() public {
        _seedMatured(1e20);
        ITouchstoneHook(hook).refreshRef();
        refPool.setMode(MockRefPool.Mode.Revert);
        vm.prank(curator);
        vm.expectRevert(ITouchstoneHook.RefUnreadable.selector);
        hv.rebalanceSwap(true, 1e16, type(uint256).max);
        refPool.setMode(MockRefPool.Mode.Ok);
        _swapAs(true, 1e16, type(uint256).max); // control: readable, the same action passes
    }

    /// @notice The action reads the live reference, not the hook's cache: the cache says 0, the live TWAP 200 puts
    ///         spot outside the fence.
    function test_U_fence_live__actionReadsTheLiveReferenceNotTheCache() public {
        ITouchstoneHook(hook).refreshRef();
        assertEq(_obs().refTick, 0, "cache at 0");
        _setRef(200);
        vm.prank(curator);
        vm.expectRevert(VaultCommon.Fence.selector);
        hv.rebalanceSwap(true, 1e16, type(uint256).max);
    }

    // ------------------------------------------------------------------ U-guard

    /// @notice The fence `|tick_spot − refTick| ≤ REF_BAND` on both sides, at the bound.
    function test_U_guard__fenceOnBothSides() public {
        int24 band = tier.REF_BAND;
        int24[4] memory refs = [band, -band, band + 1, -band - 1];
        for (uint256 i; i < 4; ++i) {
            uint256 snap = vm.snapshotState();
            _setRef(refs[i]);
            if (i < 2) {
                _swapAs(true, 1e16, type(uint256).max);
            } else {
                vm.prank(curator);
                vm.expectRevert(VaultCommon.Fence.selector);
                hv.rebalanceSwap(true, 1e16, type(uint256).max);
            }
            vm.revertToState(snap);
        }
    }

    /// @notice Only the curator acts.
    function test_U_guard__onlyCurator() public {
        vm.prank(alice);
        vm.expectRevert(VaultCommon.OnlyCurator.selector);
        hv.rebalanceSwap(true, 1e16, type(uint256).max);
    }

    /// @notice `COOLDOWN` blocks since the last action (none before the first): one block short reverts, at the bound
    ///         it passes.
    function test_U_guard__cooldownSinceTheLastAction() public {
        _swapAs(true, 1e16, type(uint256).max);
        advance(tier.COOLDOWN - 1);
        vm.prank(curator);
        vm.expectRevert(VaultCommon.Cooldown.selector);
        hv.rebalanceSwap(true, 1e16, type(uint256).max);
        advance(1);
        _swapAs(true, 1e16, type(uint256).max);
    }

    /// @notice No action over a pending record: blocks that come faster than the tier's nominal spacing pass the
    ///         cooldown before the markout is due in time; the action reverts `MarkoutPending` even when the reserve
    ///         holds no shares (so `Op.Charge`'s own check cannot fire). Control: at the nominal spacing it settles first.
    function test_U_settle__noActionOverAPendingRecord() public {
        _swapAs(true, 1e16, type(uint256).max);
        uint256 snap = vm.snapshotState();
        vm.roll(vm.getBlockNumber() + tier.COOLDOWN); // blocks, no time: the window is not due
        Bank memory bk = hv.bankFull();
        (bk.bondShares, bk.reserveShares, bk.reserveBond) = (bk.bondShares + bk.reserveShares, 0, 0);
        hv.setBank(bk);
        vm.prank(curator);
        vm.expectRevert(VaultCommon.MarkoutPending.selector);
        hv.rebalanceSwap(true, 1e16, type(uint256).max);
        vm.revertToState(snap);
        advance(tier.COOLDOWN); // the nominal spacing: due, settled first
        _swapAs(true, 1e16, type(uint256).max);
    }

    /// @notice At most `MAX_SWAPS` per epoch: the last allowed one passes and counts; one more reverts `MaxSwaps`.
    function test_U_guard__maxSwapsPerEpoch() public {
        hv.settle();
        Scalars memory s = hv.scalars();
        _setCounters(0, tier.MAX_SWAPS - 1, s.nav0, s.delta0);
        uint256 snap = vm.snapshotState();
        _swapAs(true, 1e16, type(uint256).max);
        assertEq(hv.scalars().swapCount, tier.MAX_SWAPS, "counted");
        vm.revertToState(snap);
        _setCounters(0, tier.MAX_SWAPS, s.nav0, s.delta0);
        vm.prank(curator);
        vm.expectRevert(VaultCommon.MaxSwaps.selector);
        hv.rebalanceSwap(true, 1e16, type(uint256).max);
    }

    /// @notice The per-action cap at the reference on the holdings before the action (not at spot, not on `NAV₀`):
    ///         the largest amount within `MAX_SWAP_ACTION` passes, one more unit reverts `SwapCap`.
    function test_U_guard__perActionCapAtTheReferenceOnTheHoldings() public {
        _setRef(50); // the reference above spot: a cap valued at spot would allow more
        (Holdings memory b,) = _prime();
        Scalars memory s = hv.scalars();
        _setCounters(0, 0, s.nav0 / 2, s.delta0); // a cap on NAV₀ would allow less
        uint256 amt = _capAmount0(b, tier.MAX_SWAP_ACTION, 50);
        uint256 snap = vm.snapshotState();
        _swapAs(true, uint128(amt), type(uint256).max);
        vm.revertToState(snap);
        vm.prank(curator);
        vm.expectRevert(VaultCommon.SwapCap.selector);
        hv.rebalanceSwap(true, uint128(amt + 1), type(uint256).max);
    }

    /// @notice The epoch cap: the counter plus this action's fraction (rounded up) at most `MAX_SWAP_EPOCH`.
    function test_U_guard__perEpochCap() public {
        (Holdings memory b,) = _prime();
        uint256 amt = 1e16;
        uint256 f = _frac(b, true, amt, 0);
        Scalars memory s = hv.scalars();
        uint256 snap = vm.snapshotState();
        _setCounters(uint64(tier.MAX_SWAP_EPOCH - f), 0, s.nav0, s.delta0);
        _swapAs(true, uint128(amt), type(uint256).max);
        assertEq(hv.scalars().epochSwapFrac, tier.MAX_SWAP_EPOCH, "the counter adds the fraction");
        vm.revertToState(snap);
        _setCounters(uint64(tier.MAX_SWAP_EPOCH - f + 1), 0, s.nav0, s.delta0);
        vm.prank(curator);
        vm.expectRevert(VaultCommon.SwapCap.selector);
        hv.rebalanceSwap(true, uint128(amt), type(uint256).max);
    }

    /// @notice An `amountIn` above idle reverts `SwapCap` (the vault holds almost no idle token1 here).
    function test_U_guard__swapCapAboveIdle() public {
        Holdings memory h = hv.holdings();
        assertLt(h.idle1, 1e15, "little idle token1");
        vm.prank(curator);
        vm.expectRevert(VaultCommon.SwapCap.selector);
        hv.rebalanceSwap(false, h.idle1 + 1, type(uint256).max);
    }

    /// @notice The adverse limit `tick_spot − (FEE_TICKS + BAND_SWAP)`: against thin liquidity the swap stops at the
    ///         limit, fills partially, is charged on the filled result and counts only the filled fraction.
    function test_U_guard__adverseLimitPartialFillCountsTheFilledAmount() public {
        _removeThirdPartyLiquidity();
        (Holdings memory b,) = _prime();
        int24 t0 = _tick();
        uint256 amt = 2e16;
        (uint256 g0,) = manager.getFeeGrowthInside(poolId, LO, HI);
        _swapAs(true, uint128(amt), type(uint256).max);
        (uint256 g1,) = manager.getFeeGrowthInside(poolId, LO, HI);
        int24 lim = t0 - (tier.FEE_TICKS + tier.BAND_SWAP);
        assertEq(_sqrtSpot(), TickMath.getSqrtPriceAtTick(lim), "stopped at the adverse limit");
        Holdings memory a = hv.holdings();
        // the vault is the only liquidity: the token0 fee it paid came back to its idle through the collect
        uint256 fee0;
        unchecked {
            fee0 = FullMath.mulDiv(g1 - g0, b.L, 1 << 128);
        }
        assertGt(fee0, 0, "the fee came back");
        uint256 filled = b.idle0 - a.idle0 + fee0;
        assertLt(filled, amt, "a partial fill");
        assertGt(filled, 0, "something filled");
        assertEq(hv.scalars().epochSwapFrac, _frac(b, true, filled, 0), "the counter adds the filled fraction");
        assertLt(hv.scalars().epochSwapFrac, _frac(b, true, amt, 0), "less than the requested fraction");
        assertEq(hv.scalars().swapCount, 1, "one swap");
    }

    /// @notice `rebalanceSwap` never changes the mode: from BREACHED it stays BREACHED; in EXITED it reverts.
    function test_U_guard__rebalanceSwapKeepsTheMode() public {
        _setRef(HI + 1000);
        _driveBreaches(1);
        _setRef(0);
        hv.settle(); // the checkpoint processes the breach
        (,,,,, uint8 m0,) = hv.bank();
        assertEq(m0, 1, "BREACHED");
        _swapAs(true, 1e16, type(uint256).max);
        (,,,,, uint8 m1,) = hv.bank();
        assertEq(m1, 1, "still BREACHED");
        _setRef(HI + 1000);
        _driveBreaches(2);
        _setRef(0);
        advance(tier.COOLDOWN);
        vm.prank(curator);
        vm.expectRevert(Ladder.BadMode.selector);
        hv.rebalanceSwap(true, 1e16, type(uint256).max);
    }

    /// @notice The burn comes from unvested pay first (the bank), then the bond, and the event reports both.
    function test_U_3__burnFromUnvestedPayFirst() public {
        _setRef(40);
        uint256 tiny = 1;
        Bank memory bk = hv.bankFull();
        uint32 e = uint32(vm.getBlockNumber() / tier.EPOCH);
        bk.cohorts[e % RING] = Cohort(e, uint128(tiny));
        bk.bondShares -= uint128(tiny);
        hv.setBank(bk);
        vm.recordLogs();
        _swapAs(true, 2e16, type(uint256).max);
        (,, uint256 bb, uint256 bd) = _actionLog();
        assertEq(bb, tiny, "the cohort first");
        assertGt(bd, 0, "then the bond");
    }

    // ------------------------------------------------------------------ helpers

    function _actionLog() internal returns (uint256 c, uint160 p, uint256 bb, uint256 bd) {
        bytes32 sig = keccak256("Action(uint8,uint256,uint160,uint256,uint256)");
        Vm.Log[] memory logs = vm.getRecordedLogs();
        uint256 n;
        for (uint256 i; i < logs.length; ++i) {
            if (logs[i].emitter == address(hv) && logs[i].topics[0] == sig) {
                uint8 kind;
                (kind, c, p, bb, bd) = abi.decode(logs[i].data, (uint8, uint256, uint160, uint256, uint256));
                assertEq(kind, KIND_SWAP, "kind");
                ++n;
            }
        }
        assertEq(n, 1, "one Action event");
    }

    function _swapEventPaid0() internal returns (uint256) {
        bytes32 sig = keccak256("Swap(bytes32,address,int128,int128,uint160,uint128,int24,uint24)");
        Vm.Log[] memory logs = vm.getRecordedLogs();
        for (uint256 i; i < logs.length; ++i) {
            if (logs[i].emitter == address(manager) && logs[i].topics[0] == sig) {
                (int128 a0,,,,,) = abi.decode(logs[i].data, (int128, int128, uint160, uint128, int24, uint24));
                return uint256(int256(-a0));
            }
        }
        revert("no Swap event");
    }

    function _seedMatured(uint128 amt) internal {
        Bank memory bk = hv.bankFull();
        bk.maturedPending += amt;
        bk.bondShares -= amt;
        hv.setBank(bk);
    }

    /// @notice Leaves exactly `n` bond shares (the rest become vested-unclaimed pay, still held by the vault).
    function _setBond(uint256 n) internal {
        Bank memory bk = hv.bankFull();
        bk.vestedUnclaimed += uint128(bk.bondShares - n);
        bk.bondShares = uint128(n);
        hv.setBank(bk);
    }

    function _eqH(Holdings memory x, Holdings memory y, string memory why) internal pure {
        assertEq(x.idle0, y.idle0, string.concat(why, ": idle0"));
        assertEq(x.idle1, y.idle1, string.concat(why, ": idle1"));
        assertEq(x.L, y.L, string.concat(why, ": L"));
        assertEq(x.lo, y.lo, string.concat(why, ": lo"));
        assertEq(x.hi, y.hi, string.concat(why, ": hi"));
    }

    function token0Id() internal view returns (uint256) {
        return uint256(uint160(address(token0)));
    }

    function token1Id() internal view returns (uint256) {
        return uint256(uint160(address(token1)));
    }
}

/// @notice The vault with no position (L = 0, all idle): `rebalanceSwap` skips the collect after the swap (a zero-delta
///         modify reverts on L = 0) and is charged on the idle holdings.
contract VaultActionUnplacedTest is LiquidPoolBase {
    function test_U_3__noCollectAfterTheSwapAtZeroLiquidity() public {
        assertEq(hv.holdings().L, 0, "no position");
        hv.settle();
        Holdings memory b = hv.holdings();
        vm.prank(curator);
        hv.rebalanceSwap(true, 1e16, type(uint256).max);
        (, Holdings memory pb, Holdings memory pa,,) = hv.pendingAction();
        assertEq(pb.idle0, b.idle0, "before");
        assertEq(pa.L, 0, "still no position");
        assertLt(pa.idle0, b.idle0, "swapped from idle");
    }
}

/// @notice The USDC/WETH-like pool (tick 196,000; token0 at 6 decimals, token1 at 18): the caps and the charge in the
///         `q` form where a token0 unit is worth ≈ 3.3e8 token1 units, so a cap valued in the wrong token fails.
contract VaultActionHighTickTest is LiquidPoolBase {
    using StateLibrary for *;

    uint256 internal constant WAD = 1e18;
    int24 internal constant C = 196_000;

    function _highTick() internal pure override returns (bool) {
        return true;
    }

    function setUp() public override {
        super.setUp();
        hv.placeForTest(C - 1800, C + 200); // most token0 stays idle
        _setRef(C);
    }

    function _setRef(int24 t) internal {
        uint32[] memory s = new uint32[](1);
        int24[] memory k = new int24[](1);
        (s[0], k[0]) = (1, t);
        refPool.setPath(s, k);
    }

    function _capAmount0(Holdings memory h, uint256 capFrac, int24 ref) internal view returns (uint256 a) {
        uint160 sr = TickMath.getSqrtPriceAtTick(ref);
        (uint256 nav,) = ExecLedger.value(h, sr);
        uint256 lim = FullMath.mulDiv(capFrac, nav, WAD);
        uint256 hiA = h.idle0;
        while (a < hiA) {
            uint256 mid = (a + hiA + 1) / 2;
            (uint256 v,) = ExecLedger.value(Holdings(uint128(mid), 0, 0, 0, 0), sr);
            if (v <= lim) a = mid;
            else hiA = mid - 1;
        }
    }

    /// @notice The per-action cap at the reference in the q form: the largest token0 amount within the cap passes,
    ///         one more unit (≈ 3.3e8 token1 units) reverts `SwapCap`.
    function test_U_guard__perActionCapAtTheReferenceUsdcWeth() public {
        _setRef(C + 50);
        hv.settle();
        Holdings memory b = hv.holdings();
        uint256 amt = _capAmount0(b, tier.MAX_SWAP_ACTION, C + 50);
        assertLt(amt, b.idle0, "the cap binds before idle");
        uint256 snap = vm.snapshotState();
        vm.prank(curator);
        hv.rebalanceSwap(true, uint128(amt), type(uint256).max);
        vm.revertToState(snap);
        vm.prank(curator);
        vm.expectRevert(VaultCommon.SwapCap.selector);
        hv.rebalanceSwap(true, uint128(amt + 1), type(uint256).max);
    }

    /// @notice The charge, the reserve and the epilogue at the USDC/WETH-like price, exact against `ExecLedger`.
    function test_U_3__chargeExactUsdcWeth() public {
        _setRef(C + 40);
        hv.settle();
        Holdings memory b = hv.holdings();
        uint256 S = hv.totalSupply();
        (uint160 s0,,,) = manager.getSlot0(poolId);
        vm.prank(curator);
        hv.rebalanceSwap(true, 5e7, type(uint256).max);
        (, Holdings memory pb, Holdings memory pa, uint128 c0, uint128 r) = hv.pendingAction();
        assertEq(keccak256(abi.encode(pb)), keccak256(abi.encode(b)), "before");
        uint160 sr = TickMath.getSqrtPriceAtTick(C + 40);
        (uint256 navSpot,) = ExecLedger.value(b, s0);
        uint256 reserve = FullMath.mulDiv(tier.RESERVE_RATIO, navSpot, WAD);
        (uint256 charge,,, uint256 burn, uint256 burnR,) = ExecLedger.measure(b, pa, s0, s0, sr, reserve, S, 0, 0);
        assertGt(charge, 0, "a charge");
        assertEq(c0, charge, "charge");
        assertEq(r, reserve, "reserve");
        assertEq(hv.totalSupply(), S - burn, "burn");
        assertEq(hv.bankFull().reserveShares, burnR, "reserve shares");
        (uint256 nav,) = ExecLedger.value(pa, sr);
        assertEq(hv.scalars().nav0, nav, "NAV0 at the reference");
    }
}
