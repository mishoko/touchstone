// SPDX-License-Identifier: MIT
pragma solidity 0.8.26;

import {Vm} from "forge-std/Vm.sol";
import {StateLibrary} from "@uniswap/v4-core/src/libraries/StateLibrary.sol";
import {TickMath} from "@uniswap/v4-core/src/libraries/TickMath.sol";
import {FullMath} from "@uniswap/v4-core/src/libraries/FullMath.sol";
import {SqrtPriceMath} from "@uniswap/v4-core/src/libraries/SqrtPriceMath.sol";
import {SwapParams, ModifyLiquidityParams} from "@uniswap/v4-core/src/types/PoolOperation.sol";
import {PoolSwapTest} from "@uniswap/v4-core/src/test/PoolSwapTest.sol";
import {LiquidPoolBase} from "../utils/LiquidPoolBase.sol";
import {Scalars} from "../utils/VaultHarness.sol";
import {TouchstoneVault} from "../../src/TouchstoneVault.sol";
import {ITouchstoneHook} from "../../src/interfaces/ITouchstoneHook.sol";
import {ExecLedger} from "../../src/libraries/ExecLedger.sol";
import {FeeBank} from "../../src/libraries/FeeBank.sol";
import {Ladder} from "../../src/libraries/Ladder.sol";
import {Markout} from "../../src/types/Markout.sol";
import {Holdings} from "../../src/types/Holdings.sol";
import {PreSwap} from "../../src/types/PreSwap.sol";
import {Bank} from "../../src/types/Bank.sol";
import {SettleExtraOracle} from "../utils/SettleExtraOracle.sol";
import {IVaultEntries} from "../../src/interfaces/ITouchstoneVaultExtension.sol";
import {VaultCommon} from "../../src/TouchstoneVaultBase.sol";

/// @notice The curator's `setPositions` on the real hook over the scripted reference pool, from an unplaced vault (all
///         idle, about half of each token at tick 0). Expectations come from `ExecLedger` (checked bit for bit against
///         the reference model's vectors) on inputs the test reads itself: the holdings before the action, the pool's
///         spot before and after the vault's move, the PoolManager's `Swap` events, the scripted reference tick and
///         the supply.
contract VaultPlaceTest is LiquidPoolBase, SettleExtraOracle {
    using StateLibrary for *;

    event Action(uint8 kind, uint256 charge, uint160 pStar, uint256 burnedBank, uint256 burnedBond);
    event RangeRegistered(int24 lo, int24 hi, int24 liveTick);

    uint256 internal constant WAD = 1e18;
    uint8 internal constant KIND_PLACE = 2; // uint8(Entry.SetPositions)

    struct Exp {
        uint256 charge;
        uint160 pStar;
        uint256 navBurn;
        uint256 burn;
        uint256 burnR;
        uint256 reserve;
    }

    function setUp() public override {
        super.setUp();
        _setRef(0);
    }

    // ------------------------------------------------------------------ helpers

    function _setRef(int24 t) internal {
        uint32[] memory s = new uint32[](1);
        int24[] memory k = new int24[](1);
        (s[0], k[0]) = (1, t);
        refPool.setPath(s, k);
    }

    function _none() internal pure returns (PreSwap memory) {
        return PreSwap(false, false, 0);
    }

    function _sqrt(int24 t) internal pure returns (uint160) {
        return TickMath.getSqrtPriceAtTick(t);
    }

    function _sqrtSpot() internal view returns (uint160 sp) {
        (sp,,,) = manager.getSlot0(poolId);
    }

    function _place(int24 lo, int24 hi, PreSwap memory ps, uint160 moveTo) internal {
        vm.prank(curator);
        hv.setPositions(lo, hi, ps, moveTo, type(uint256).max);
    }

    function _placeReverts(int24 lo, int24 hi, PreSwap memory ps, uint160 moveTo, bytes4 sel) internal {
        vm.prank(curator);
        vm.expectRevert(sel);
        hv.setPositions(lo, hi, ps, moveTo, type(uint256).max);
    }

    /// @notice Runs the prologue in this block, so the holdings read next are the action's `before`.
    function _prime() internal returns (Holdings memory before, uint256 S) {
        hv.settle();
        return (hv.holdings(), hv.totalSupply());
    }

    function _expect(Holdings memory b, Holdings memory a, uint160 s0, uint160 sSpot, int24 ref, uint256 S)
        internal
        view
        returns (Exp memory e)
    {
        (uint256 navSpot,) = ExecLedger.value(b, sSpot);
        e.reserve = FullMath.mulDiv(tier.RESERVE_RATIO, navSpot, WAD);
        (e.charge, e.pStar, e.navBurn, e.burn, e.burnR,) =
            ExecLedger.measure(b, a, s0, sSpot, _sqrt(ref), e.reserve, S, 0, 0);
    }

    function _setCounters(uint64 frac, uint16 count) internal {
        Scalars memory s = hv.scalars();
        hv.setScalars(s.nav0, s.delta0, s.tauV0, s.segStart, s.baseOwed, s.epoch, frac, count);
    }

    function _removeThirdPartyLiquidity() internal {
        lpRouter.modifyLiquidity(key, ModifyLiquidityParams(-6000, 6000, -int256(_thirdPartyL()), 0), "");
    }

    /// @notice A third-party exact-input swap that stops at `tick` (the market moving spot).
    function _pushTo(int24 tick) internal {
        bool z = tick < _tick();
        swapRouter.swap(
            key,
            SwapParams(z, -int256(1e30), _sqrt(tick)),
            PoolSwapTest.TestSettings({takeClaims: false, settleUsingBurn: false}),
            ""
        );
    }

    /// @notice Credits the vault's idle (test seam) from this test contract's tokens.
    function _credit(uint256 a0, uint256 a1) internal {
        token0.approve(address(hv), type(uint256).max);
        token1.approve(address(hv), type(uint256).max);
        hv.creditIdle(a0, a1);
    }

    function _fracOf(Holdings memory h, bool z, uint256 amt, int24 ref) internal view returns (uint256) {
        (uint256 nav,) = ExecLedger.value(h, _sqrt(ref));
        (uint256 v,) = ExecLedger.value(Holdings(z ? uint128(amt) : 0, z ? 0 : uint128(amt), 0, 0, 0), _sqrt(ref));
        return FullMath.mulDivRoundingUp(v, WAD, nav);
    }

    /// @notice The vault's swap deltas from the PoolManager's `Swap` events of the recorded logs whose sender is the
    ///         vault, in order (the swapper's view: negative = paid in).
    function _vaultSwaps() internal returns (int128[] memory d0, int128[] memory d1) {
        bytes32 sig = keccak256("Swap(bytes32,address,int128,int128,uint160,uint128,int24,uint24)");
        Vm.Log[] memory logs = vm.getRecordedLogs();
        uint256 n;
        for (uint256 i; i < logs.length; ++i) {
            if (logs[i].emitter == address(manager) && logs[i].topics[0] == sig) ++n;
        }
        (d0, d1) = (new int128[](n), new int128[](n));
        n = 0;
        for (uint256 i; i < logs.length; ++i) {
            if (logs[i].emitter == address(manager) && logs[i].topics[0] == sig) {
                assertEq(address(uint160(uint256(logs[i].topics[2]))), address(hv), "the vault swapped");
                (d0[n], d1[n],,,,) = abi.decode(logs[i].data, (int128, int128, uint160, uint128, int24, uint24));
                ++n;
            }
        }
    }

    struct BalanceDeltaQuote {
        uint256 paid;
    }

    /// @notice A router exact-input swap with the pre-swap's limit (`limitTicks` from the current tick); returns the
    ///         input paid (call between snapshot and revert).
    function _quote(bool z, uint256 amt, int24 limitTicks) internal returns (BalanceDeltaQuote memory q) {
        int24 lim = z ? _tick() - limitTicks : _tick() + limitTicks;
        uint256 b = z ? token0.balanceOf(address(this)) : token1.balanceOf(address(this));
        swapRouter.swap(
            key,
            SwapParams(z, -int256(amt), _sqrt(lim)),
            PoolSwapTest.TestSettings({takeClaims: false, settleUsingBurn: false}),
            ""
        );
        q.paid = b - (z ? token0.balanceOf(address(this)) : token1.balanceOf(address(this)));
    }

    function _positionL(int24 lo, int24 hi) internal view returns (uint128 l) {
        (l,,) = manager.getPositionInfo(poolId, address(hv), lo, hi, bytes32(0));
    }

    function _mode() internal view returns (uint8 m) {
        (,,,,, m,) = hv.bank();
    }

    function _eqH(Holdings memory x, Holdings memory y, string memory why) internal pure {
        assertEq(x.idle0, y.idle0, string.concat(why, ": idle0"));
        assertEq(x.idle1, y.idle1, string.concat(why, ": idle1"));
        assertEq(x.L, y.L, string.concat(why, ": L"));
        assertEq(x.lo, y.lo, string.concat(why, ": lo"));
        assertEq(x.hi, y.hi, string.concat(why, ": hi"));
    }

    // ------------------------------------------------------------------ the placement

    /// @notice A placement on `[lo, hi)`: the maximum liquidity the idle inventory supports at spot (less the headroom)
    ///         is added on the range in the PoolManager, the claims pay for it, the charge is the shortfall at the worse
    ///         leg, and the `Action` event is kind 2.
    function test_U_3__placementAddsTheSizedLiquidityAndIsCharged() public {
        _setRef(30);
        (Holdings memory b, uint256 S) = _prime();
        uint160 s0 = _sqrtSpot();
        (uint128 L, Holdings memory want,) = ExecLedger.placementSize(b, -1000, 1000, s0, _sqrt(30));
        vm.recordLogs();
        _place(-1000, 1000, _none(), 0);
        Holdings memory a = hv.holdings();
        _eqH(a, want, "the sized holdings");
        assertGt(L, 0, "liquidity");
        assertEq(_positionL(-1000, 1000), L, "the PoolManager position");
        assertEq(manager.balanceOf(address(hv), uint256(uint160(address(token0)))), a.idle0, "claims0 = idle0");
        assertEq(manager.balanceOf(address(hv), uint256(uint160(address(token1)))), a.idle1, "claims1 = idle1");
        Exp memory e = _expect(b, a, s0, s0, 30, S);
        (, Holdings memory pb, Holdings memory pa, uint128 c0, uint128 r) = hv.pendingAction();
        _eqH(pb, b, "record before");
        _eqH(pa, a, "record after");
        assertEq(c0, e.charge, "charge");
        assertEq(r, e.reserve, "reserve");
        assertEq(hv.totalSupply(), S - e.burn, "the burn");
        assertEq(hv.bankFull().reserveShares, e.burnR, "the reserve's shares");
        Vm.Log[] memory logs = vm.getRecordedLogs();
        bytes32 sig = keccak256("Action(uint8,uint256,uint160,uint256,uint256)");
        uint256 n;
        for (uint256 i; i < logs.length; ++i) {
            if (logs[i].emitter == address(hv) && logs[i].topics[0] == sig) {
                (uint8 kind, uint256 c,,,) = abi.decode(logs[i].data, (uint8, uint256, uint160, uint256, uint256));
                assertEq(kind, KIND_PLACE, "kind");
                assertEq(c, e.charge, "event charge");
                ++n;
            }
        }
        assertEq(n, 1, "one Action event");
    }

    /// @notice The range is registered with the hook at the live reference (a new clock run; the cache takes the
    ///         live tick), not at spot.
    function test_U_guard__registersTheRangeAtTheLiveReference() public {
        _setRef(30);
        vm.expectEmit(hook);
        emit RangeRegistered(-1000, 1000, 30);
        _place(-1000, 1000, _none(), 0);
        assertEq(_obs().refTick, 30, "the cache takes the live tick");
        assertFalse(_obs().emptyRange, "no longer EMPTY");
    }

    /// @notice Only the curator places.
    function test_U_guard__onlyCuratorPlaces() public {
        vm.prank(alice);
        vm.expectRevert(VaultCommon.OnlyCurator.selector);
        hv.setPositions(-1000, 1000, _none(), 0, type(uint256).max);
    }

    // ------------------------------------------------------------------ U-guard: the placement rows

    /// @notice Width exactly W, aligned to the spacing, containing spot and the reference (half-open at hi).
    function test_U_guard__placementRows() public {
        _setRef(50); // spot 0, the reference 50: inside the fence
        _placeReverts(-1000, 990, _none(), 0, VaultCommon.Width.selector);
        _placeReverts(-1000, 1010, _none(), 0, VaultCommon.Width.selector);
        _placeReverts(-995, 1005, _none(), 0, VaultCommon.Alignment.selector);
        _placeReverts(10, 2010, _none(), 0, VaultCommon.NotContained.selector); // spot 0 below lo
        _placeReverts(-1990, 10, _none(), 0, VaultCommon.NotContained.selector); // the reference 50 at or above hi
        _placeReverts(-1950, 50, _none(), 0, VaultCommon.NotContained.selector); // the reference exactly at hi
        _place(-1940, 60, _none(), 0); // control: the same rules met
        assertGt(hv.holdings().L, 0, "placed");
    }

    /// @notice The fence runs on the post-move spot, not on `s0`: from a spot outside the fence the vault's own move
    ///         brings it inside and the placement passes; from a spot inside the fence a move outside reverts `Fence`.
    function test_U_guard__fenceOnThePostMoveSpotNotS0() public {
        _removeThirdPartyLiquidity(); // the move crosses an empty region
        _setRef(200);
        uint256 snap = vm.snapshotState();
        _place(-800, 1200, _none(), _sqrt(180));
        assertEq(_sqrtSpot(), _sqrt(180), "moved");
        vm.revertToState(snap);
        _setRef(0);
        _placeReverts(-1000, 1000, _none(), _sqrt(100), VaultCommon.Fence.selector);
    }

    /// @notice An active pre-swap with `MAX_SWAPS` exhausted reverts `MaxSwaps`; the same placement without one passes.
    function test_U_guard__preSwapWithMaxSwapsExhaustedReverts() public {
        hv.settle();
        _setCounters(0, tier.MAX_SWAPS);
        uint256 snap = vm.snapshotState();
        _placeReverts(-1000, 1000, PreSwap(true, true, 1e16), 0, VaultCommon.MaxSwaps.selector);
        vm.revertToState(snap);
        _place(-1000, 1000, _none(), 0);
    }

    /// @notice The pre-swap counts toward the caps and `MAX_SWAPS`: above the per-action cap (valued at the reference
    ///         on the holdings before the action) it reverts `SwapCap`; within it, the counters add its fraction and one
    ///         swap.
    function test_U_guard__preSwapCountsTowardTheCaps() public {
        (Holdings memory b,) = _prime();
        uint256 amt = 2e16;
        uint256 f = _fracOf(b, true, amt, 0);
        uint256 snap = vm.snapshotState();
        _place(-1000, 1000, PreSwap(true, true, uint128(amt)), 0);
        assertEq(hv.scalars().epochSwapFrac, f, "the epoch counter adds the filled fraction");
        assertEq(hv.scalars().swapCount, 1, "one swap");
        vm.revertToState(snap);
        _setCounters(uint64(tier.MAX_SWAP_EPOCH - f + 1), 0);
        _placeReverts(-1000, 1000, PreSwap(true, true, uint128(amt)), 0, VaultCommon.SwapCap.selector);
    }

    /// @notice Containment is checked on the pre-action spot (post-move, pre-swap): a range whose lower edge is spot
    ///         passes although the pre-swap takes spot below it, and L is sized at the post-pre-swap spot.
    function test_U_guard__containmentOnThePreActionSpot() public {
        _credit(9e18, 0); // mostly token0: a range above spot deploys it
        assertEq(_tick(), 0, "spot at the lower edge");
        _place(0, 2000, PreSwap(true, true, 1e16), 0);
        assertLt(_tick(), 0, "the pre-swap took spot below the range");
        assertGt(hv.holdings().L, 0, "placed");
    }

    /// @notice L is sized at the post-pre-swap spot: the placement's L equals `placementSize` on the idle the
    ///         pre-swap left (from the PoolManager's `Swap` event) at the spot after it, which differs from the size at
    ///         the spot before it (control).
    function test_U_guard__liquiditySizedAtThePostPreSwapSpot() public {
        (Holdings memory b,) = _prime();
        uint160 s0 = _sqrtSpot();
        vm.recordLogs();
        _place(-1000, 1000, PreSwap(true, true, 2e16), 0);
        (int128[] memory d0, int128[] memory d1) = _vaultSwaps();
        assertEq(d0.length, 1, "one vault swap (the pre-swap)");
        Holdings memory mid =
            Holdings(uint128(int128(b.idle0) + d0[0]), uint128(int128(b.idle1) + d1[0]), 0, b.lo, b.hi);
        uint160 sNow = _sqrtSpot();
        assertLt(sNow, s0, "the pre-swap moved spot");
        (uint128 want,,) = ExecLedger.placementSize(mid, -1000, 1000, sNow, _sqrt(0));
        (uint128 atPre,,) = ExecLedger.placementSize(mid, -1000, 1000, s0, _sqrt(0));
        assertEq(hv.holdings().L, want, "L at the post-pre-swap spot");
        assertNotEq(want, atPre, "control: the size at the pre-swap spot differs");
    }

    /// @notice An edge placement with spot one unit inside the edge and the other token idle sizes without reverting:
    ///         the token1 leg over one sqrt unit saturates instead of overflowing.
    function test_U_guard__edgePlacementSizesWithoutReverting() public {
        _credit(9e18, 0);
        uint160 moveTo = _sqrt(0) + 1; // approached from below: the pool tick stays at lo = 0
        _place(0, 2000, _none(), moveTo);
        assertEq(_sqrtSpot(), moveTo, "one unit inside the lower edge");
        assertEq(_tick(), 0, "the pool tick equals lo");
        assertGt(hv.holdings().L, 0, "placed");
        assertGe(hv.scalars().delta0, tier.DELTA_MIN, "deployed");
    }

    /// @notice `δ_after ≥ DELTA_MIN`: exactly `DELTA_MIN` passes, one token1 wei more idle reverts `DeltaMin` (the
    ///         placement's liquidity is bound by token0, so extra idle token1 only lowers δ).
    function test_U_guard__deltaMinBoundary() public {
        _credit(0, 1e17); // token0 binds
        (Holdings memory b,) = _prime();
        uint160 s0 = _sqrtSpot();
        (, Holdings memory a, uint64 d) = ExecLedger.placementSize(b, -1000, 1000, s0, s0);
        assertGt(d, WAD / 2, "deployed above DELTA_MIN before the credit");
        (uint256 nav, uint256 pv) = ExecLedger.value(a, s0);
        uint256 extra = 2 * pv - nav; // NAV' = 2·pv: δ = WAD/2 exactly
        uint256 snap = vm.snapshotState();
        _credit(0, extra);
        (,, d) = ExecLedger.placementSize(hv.holdings(), -1000, 1000, s0, s0);
        assertEq(d, WAD / 2, "precondition: delta exactly DELTA_MIN");
        _place(-1000, 1000, _none(), 0);
        vm.revertToState(snap);
        _credit(0, extra + 1);
        (,, d) = ExecLedger.placementSize(hv.holdings(), -1000, 1000, s0, s0);
        assertEq(d, WAD / 2 - 1, "precondition: one below");
        _placeReverts(-1000, 1000, _none(), 0, VaultCommon.DeltaMin.selector);
    }

    /// @notice A placement the idle cannot fund at all (L = 0: the pre-swap spent every token1 and spot is inside the
    ///         range) reverts `DeltaMin`, not the PoolManager's empty-position error (no zero add is attempted).
    function test_U_guard__zeroLiquidityPlacementRevertsDeltaMin() public {
        _credit(60e18, 0); // idle token1 is now below the per-action cap
        // deep enough that the pre-swap of all idle token1 fills within its limit (idle token1 ends at exactly 0)
        lpRouter.modifyLiquidity(key, ModifyLiquidityParams(-6000, 6000, 1e23, 0), "");
        Holdings memory h = hv.holdings();
        // precondition: the same exact-input swap within the pre-swap's limit consumes all of idle token1
        uint256 snap = vm.snapshotState();
        BalanceDeltaQuote memory q = _quote(false, h.idle1, 10);
        assertEq(q.paid, h.idle1, "precondition: the pre-swap fills exactly");
        vm.revertToState(snap);
        _placeReverts(-1000, 1000, PreSwap(true, false, h.idle1), 0, VaultCommon.DeltaMin.selector);
    }

    // ------------------------------------------------------------------ U-settle: the vault's move

    /// @notice The move reaches `moveTo` (exact output, limit `moveTo`) and the guards run on the post-move spot.
    ///         A move that fills before its limit reverts `Move` (reached here through the harness's finite output;
    ///         with the production `type(int128).max` only an astronomically deep pool fills first).
    function test_U_settle__moveMissRevertsMove() public {
        _thinPath();
        uint256 snap = vm.snapshotState();
        _place(-1000, 1000, _none(), _sqrt(10));
        assertEq(_sqrtSpot(), _sqrt(10), "control: the move reaches moveTo");
        vm.revertToState(snap);
        hv.setMoveOut(1e10);
        _placeReverts(-1000, 1000, _none(), _sqrt(10), VaultCommon.Move.selector);
    }

    /// @dev Replaces the third-party depth with a thin position on [0, 10): a 10-tick move up costs ≈ 0.5% of NAV.
    function _thinPath() internal {
        _removeThirdPartyLiquidity();
        lpRouter.modifyLiquidity(key, ModifyLiquidityParams(0, 10, 2e19, 0), "");
    }

    /// @notice The vault's move is a trade of vault capital whenever liquidity sits on its path, so its input (valued
    ///         at the reference on the holdings before the action, like a swap) is held to `MAX_SWAP_ACTION` and counts
    ///         toward the epoch cap: liquidity placed on the path sized to cost just over the cap reverts `SwapCap`;
    ///         just under it passes and the epoch counter holds the move's fraction.
    function test_U_guard__moveCappedLikeASwap() public {
        (Holdings memory b,) = _prime();
        (uint256 nav,) = ExecLedger.value(b, _sqrt(0));
        uint256 cap = FullMath.mulDiv(tier.MAX_SWAP_ACTION, nav, WAD);
        _removeThirdPartyLiquidity();
        // the token1 a 10-tick move up through L on [0, 10) costs: L·(√p(10) − √p(0))/2^96, rounded up
        uint256 perL = uint256(_sqrt(10) - _sqrt(0));
        uint256 lOver = FullMath.mulDiv(cap, 1 << 96, perL) * 101 / 100; // ≈ 1% above the cap
        uint256 snap = vm.snapshotState();
        lpRouter.modifyLiquidity(key, ModifyLiquidityParams(0, 10, int256(lOver), 0), "");
        _placeReverts(-1000, 1000, _none(), _sqrt(10), VaultCommon.SwapCap.selector);
        vm.revertToState(snap);
        lpRouter.modifyLiquidity(key, ModifyLiquidityParams(0, 10, int256(lOver * 98 / 101), 0), ""); // ≈ 2% below
        vm.recordLogs();
        _place(-1000, 1000, _none(), _sqrt(10));
        (, int128[] memory d1) = _vaultSwaps();
        uint256 paid = uint256(int256(-d1[0]));
        assertGt(paid * 100, cap * 95, "control: the move paid close to the cap");
        assertEq(hv.scalars().epochSwapFrac, _fracOf(b, false, paid, 0), "the epoch counter holds the move");
        assertEq(hv.scalars().swapCount, 0, "a move is not a swap");
    }

    /// @notice The move and the pre-swap share one per-action budget: after a move that used a fraction f, a pre-swap
    ///         of `MAX_SWAP_ACTION − f` (rounded down) passes and one fraction unit more reverts `SwapCap`.
    function test_U_guard__moveAndPreSwapShareThePerActionBudget() public {
        _thinPath();
        (Holdings memory b,) = _prime();
        uint256 snap = vm.snapshotState();
        vm.recordLogs();
        _place(-1000, 1000, _none(), _sqrt(10));
        (, int128[] memory d1) = _vaultSwaps();
        uint256 f = _fracOf(b, false, uint256(int256(-d1[0])), 0);
        vm.revertToState(snap);
        assertGt(f, 0, "the move used part of the budget");
        // the largest token1 pre-swap whose own fraction fits the rest: value·WAD ≤ (cap − f)·NAV
        (uint256 nav,) = ExecLedger.value(b, _sqrt(0));
        uint256 amt = FullMath.mulDiv(tier.MAX_SWAP_ACTION - f, nav, WAD);
        snap = vm.snapshotState();
        _place(-1000, 1000, PreSwap(true, false, uint128(amt)), _sqrt(10));
        vm.revertToState(snap);
        _placeReverts(-1000, 1000, PreSwap(true, false, uint128(amt + nav / WAD + 1)), _sqrt(10), VaultCommon.SwapCap.selector);
        // control: the same pre-swap without the move passes (the budget is the move's, not the pre-swap's own)
        _place(-1000, 1000, PreSwap(true, false, uint128(amt + nav / WAD + 1)), 0);
    }

    /// @notice The move paid in token0 (down, at a reference where `q` is not the identity) is valued at the reference
    ///         like a token0 swap: JIT on its path sized just over the cap reverts `SwapCap`; just under it passes and
    ///         the epoch counter holds exactly the token0 fraction.
    function test_U_guard__moveDownPaidInToken0CappedAtTheReference() public {
        _setRef(-10);
        (Holdings memory b,) = _prime();
        (uint256 nav,) = ExecLedger.value(b, _sqrt(-10));
        uint256 cap = FullMath.mulDiv(tier.MAX_SWAP_ACTION, nav, WAD);
        _removeThirdPartyLiquidity();
        // the token0 value (at the reference) a 10-tick move down through L = 1e20 on [-10, 0) costs, scaled to the cap
        (uint256 v1,) = ExecLedger.value(
            Holdings(uint128(SqrtPriceMath.getAmount0Delta(_sqrt(-10), _sqrt(0), 1e20, true)), 0, 0, 0, 0), _sqrt(-10)
        );
        uint256 lCap = FullMath.mulDiv(1e20, cap, v1);
        uint256 snap = vm.snapshotState();
        lpRouter.modifyLiquidity(key, ModifyLiquidityParams(-10, 0, int256(lCap * 101 / 100), 0), "");
        _placeReverts(-1000, 1000, _none(), _sqrt(-10), VaultCommon.SwapCap.selector);
        vm.revertToState(snap);
        lpRouter.modifyLiquidity(key, ModifyLiquidityParams(-10, 0, int256(lCap * 98 / 100), 0), "");
        vm.recordLogs();
        _place(-1000, 1000, _none(), _sqrt(-10));
        (int128[] memory d0,) = _vaultSwaps();
        uint256 paid = uint256(int256(-d0[0]));
        assertEq(hv.scalars().epochSwapFrac, _fracOf(b, true, paid, -10), "the token0 fraction at the reference");
        assertNotEq(_fracOf(b, true, paid, -10), _fracOf(b, false, paid, -10), "control: token1 at par differs");
    }

    /// @notice The move counts toward the epoch cap: with the counter `MAX_SWAP_EPOCH − f` the move of fraction f
    ///         passes; one unit higher it reverts `SwapCap`.
    function test_U_guard__moveCountsTowardTheEpochCap() public {
        _thinPath();
        (Holdings memory b,) = _prime();
        uint256 snap = vm.snapshotState();
        vm.recordLogs();
        _place(-1000, 1000, _none(), _sqrt(10));
        (, int128[] memory d1) = _vaultSwaps();
        uint256 f = _fracOf(b, false, uint256(int256(-d1[0])), 0);
        vm.revertToState(snap);
        snap = vm.snapshotState();
        _setCounters(uint64(tier.MAX_SWAP_EPOCH - f), 0);
        _place(-1000, 1000, _none(), _sqrt(10));
        assertEq(hv.scalars().epochSwapFrac, tier.MAX_SWAP_EPOCH, "at the epoch cap");
        vm.revertToState(snap);
        _setCounters(uint64(tier.MAX_SWAP_EPOCH - f + 1), 0);
        _placeReverts(-1000, 1000, _none(), _sqrt(10), VaultCommon.SwapCap.selector);
    }

    /// @notice The move pays its input from idle: a move whose input exceeds idle reverts `SwapCap` before any claim
    ///         is burned.
    function test_U_settle__moveAboveIdleRevertsSwapCap() public {
        // 40 ticks up through 1e21 of third-party liquidity costs ≈ 2e18 token1; idle holds ≈ 1.1e18
        _placeReverts(-1000, 1000, _none(), _sqrt(40), VaultCommon.SwapCap.selector);
    }

    /// @notice A move already at `moveTo` is skipped (v4 rejects a price limit already reached): the placement passes
    ///         and no vault swap happens.
    function test_U_settle__moveAlreadyAtMoveToIsSkipped() public {
        vm.recordLogs();
        _place(-1000, 1000, _none(), _sqrtSpot());
        (int128[] memory d0,) = _vaultSwaps();
        assertEq(d0.length, 0, "no swap");
    }

    // ------------------------------------------------------------------ U-fence-live and the s0 leg

    /// @notice At third-party depth 0 the curator moves spot and acts in one transaction. The vault sits above its
    ///         range (all token1) after the market moved the reference to 3,000 and spot to the vault's upper edge.
    ///         In one transaction the curator re-aligns spot to the reference (an empty region: free) and places the
    ///         token1-side range `[1010, 3010)` with the vault's own move to one unit below `sqrtAt(3010)`: it passes
    ///         and the charge is below the pool fee on NAV (curvature only).
    function test_U_fence_live__depthZeroMoveAndPlaceInOneTransaction() public {
        _depthZeroAboveTheRange();
        _pushTo(3000); // the curator's re-alignment (the bundler does it in the same transaction)
        (Holdings memory b, uint256 S) = _prime();
        uint160 s0 = _sqrtSpot();
        uint160 moveTo = _sqrt(3010) - 1;
        _place(1010, 3010, _none(), moveTo);
        assertEq(_sqrtSpot(), moveTo, "moved");
        Holdings memory a = hv.holdings();
        assertGt(a.L, 0, "placed");
        Exp memory e = _expect(b, a, s0, moveTo, 3000, S);
        (,,, uint128 c0,) = hv.pendingAction();
        assertEq(c0, e.charge, "charge");
        // below the fee on NAV: the move crossed nothing, the charge is the new position's curvature over 10 ticks
        assertLe(uint256(c0) * 1e6, e.navBurn * FEE, "charge below the fee");
    }

    /// @notice The `s0` leg: without the re-alignment (spot left at the vault's old edge, 2,000 ticks below the
    ///         reference) the same move-and-place is charged at `s0`, where the new position is out of range below:
    ///         a charge of several percent of NAV, exactly `measure` with `s0` distinct from the post-move spot.
    function test_U_settle__theS0LegPricesTheMove() public {
        _depthZeroAboveTheRange();
        (Holdings memory b, uint256 S) = _prime();
        uint160 s0 = _sqrtSpot();
        uint160 moveTo = _sqrt(3010) - 1;
        _place(1010, 3010, _none(), moveTo);
        Holdings memory a = hv.holdings();
        Exp memory e = _expect(b, a, s0, moveTo, 3000, S);
        (,,, uint128 c0,) = hv.pendingAction();
        assertEq(e.pStar, s0, "the s0 leg is the worse one");
        assertEq(c0, e.charge, "charge at s0");
        assertGe(uint256(c0) * 100, e.navBurn * 2, "a charge of several percent of NAV");
    }

    /// @dev The vault on [-1000, 1000) with no third-party liquidity; the reference moves to 3,000 and the market
    ///      pushes spot through the vault's range to its upper edge (the vault is all token1).
    function _depthZeroAboveTheRange() internal {
        hv.placeForTest(-1000, 1000);
        _removeThirdPartyLiquidity();
        _setRef(3000);
        _pushTo(1000);
        assertEq(_tick(), 1000, "spot at the vault's upper edge");
    }

    // ------------------------------------------------------------------ pre-swap charged with the placement

    /// @notice The whole action is valued and charged once: the record's `before` is the holdings before the removal,
    ///         the move and the pre-swap, and the charge is `measure` on them.
    function test_U_3__preSwapChargedWithThePlacement() public {
        _setRef(20);
        (Holdings memory b, uint256 S) = _prime();
        uint160 s0 = _sqrtSpot();
        _place(-1000, 1000, PreSwap(true, true, 2e16), 0);
        (, Holdings memory pb, Holdings memory pa, uint128 c0,) = hv.pendingAction();
        _eqH(pb, b, "before = the holdings before the pre-swap");
        Exp memory e = _expect(b, pa, s0, s0, 20, S);
        assertEq(c0, e.charge, "charge");
        assertGt(c0, 0, "the pre-swap's shortfall is charged");
    }

    // ------------------------------------------------------------------ U-solv

    /// @notice The worst placement (G0 cell (c)'s shape at `REF_BAND` 60: 90% token0, spot at the fence above the
    ///         reference, the range 80 ticks below the reference, a maximum token1 pre-swap into JIT liquidity at the
    ///         limit, the reference leg) plus the reserve fits half the deployed bond ratio's cover; a bond at exactly
    ///         the burns passes the gate, one share less reverts `Insolvent` and the vault stays withdrawable.
    function test_U_solv__worstPlacementPlusReserveFitsTheBond() public {
        (uint256 c0, uint256 r, uint256 navBurn, uint256 need, PreSwap memory ps) = _worstPlacement();
        // the 2× rule on the gate's own quantity
        assertLe((c0 + r) * 2 * WAD, uint256(tier.BOND_RATIO) * navBurn, "2x cover at BOND_RATIO");
        // control: the shape is the worst one of the bond sizing ((charge + reserve)/NAV' ≥ 8.6e-4; cell (c): 8.654e-4)
        assertGe((c0 + r) * 1e7, navBurn * 8600, "the worst placement");
        uint256 snap = vm.snapshotState();
        _setBond(need);
        _place(-140, 1860, ps, 0);
        vm.revertToState(snap);
        _setBond(need - 1);
        vm.prank(curator);
        vm.expectRevert(FeeBank.Insolvent.selector);
        hv.setPositions(-140, 1860, ps, 0, type(uint256).max);
        (uint256 o0, uint256 o1) = _withdraw(alice, 1e22);
        assertGt(o0 + o1, 0, "withdrawable");
    }

    /// @dev Sets up and measures the worst placement, then reverts to the state before it. Returns the charge, the
    ///      reserve, `navBurn`, the shares the gate needs and the pre-swap.
    function _worstPlacement()
        internal
        returns (uint256 c0, uint256 r, uint256 navBurn, uint256 need, PreSwap memory ps)
    {
        _credit(9e18, 0); // ≈ 90% token0 by value
        _setRef(-tier.REF_BAND); // spot 0 at the fence above the reference
        _removeThirdPartyLiquidity();
        lpRouter.modifyLiquidity(key, ModifyLiquidityParams(0, 10, int256(JIT_L), 0), ""); // JIT at the limit
        (Holdings memory b, uint256 S) = _prime();
        uint160 s0 = _sqrtSpot();
        (uint256 nav,) = ExecLedger.value(b, _sqrt(-tier.REF_BAND));
        ps = PreSwap(true, false, uint128(FullMath.mulDiv(tier.MAX_SWAP_ACTION, nav, WAD))); // the cap, in token1
        uint256 snap = vm.snapshotState();
        _place(-140, 1860, ps, 0);
        (,, Holdings memory pa, uint128 c,) = hv.pendingAction();
        Exp memory e = _expect(b, pa, s0, s0, -tier.REF_BAND, S);
        assertEq(c, e.charge, "charge");
        assertEq(e.pStar, _sqrt(-tier.REF_BAND), "the reference leg");
        (c0, r, navBurn, need) = (e.charge, e.reserve, e.navBurn, e.burn + e.burnR);
        vm.revertToState(snap);
    }

    /// @dev The JIT size that maximises the ratio here (a sweep over 1e18…1e24 peaks at 5e20: 8.66e-4).
    uint256 internal constant JIT_L = 5e20;

    /// @notice The worst-placement shape with the vault's own move inside the shared budget: spot 70 ticks above the
    ///         reference, the move down to the fence through JIT (≈ half the per-action budget, paid in token0), then
    ///         the rest of the budget as an adverse token1 pre-swap into the same JIT; (charge + reserve) is still
    ///         covered twice at `BOND_RATIO` (§3.7: the move fills inside the fence, no worse per unit than the pre-swap).
    function test_U_solv__moveAndPreSwapInTheSharedBudgetFitTheBond() public {
        _credit(9e18, 0);
        _setRef(-70); // spot 0 is outside the fence; the move brings it to -10
        _removeThirdPartyLiquidity();
        lpRouter.modifyLiquidity(key, ModifyLiquidityParams(-10, 0, 2e20, 0), "");
        (Holdings memory b, uint256 S) = _prime();
        uint160 s0 = _sqrtSpot();
        (uint256 nav,) = ExecLedger.value(b, _sqrt(-70));
        // the move's fraction, measured in a dry run
        uint256 snap = vm.snapshotState();
        vm.recordLogs();
        _place(-150, 1850, _none(), _sqrt(-10));
        (int128[] memory d0,) = _vaultSwaps();
        uint256 f = _fracOf(b, true, uint256(int256(-d0[0])), -70);
        vm.revertToState(snap);
        assertGt(f * 4, tier.MAX_SWAP_ACTION, "the move uses a real part of the budget");
        PreSwap memory ps = PreSwap(true, false, uint128(FullMath.mulDiv(tier.MAX_SWAP_ACTION - f, nav, WAD)));
        _place(-150, 1850, ps, _sqrt(-10));
        (,, Holdings memory pa, uint128 c0, uint128 r) = hv.pendingAction();
        Exp memory e = _expect(b, pa, s0, _sqrt(-10), -70, S);
        assertEq(c0, e.charge, "charge");
        assertGt(c0, 0, "a charge");
        assertLe((uint256(c0) + r) * 2 * WAD, uint256(tier.BOND_RATIO) * e.navBurn, "2x cover at BOND_RATIO");
    }
    /// @notice The first placement at deploy: no unvested pay, so the charge and the reserve come from the bond
    ///         (`reserveBond = burnR`); deposits revert until the settlement, which returns the unburned reserve to
    ///         the bond. Spot sits `REF_BAND` from the reference.
    function test_U_solv__firstPlacementChargedFromTheBond() public {
        _setRef(tier.REF_BAND);
        (Holdings memory b, uint256 S) = _prime();
        uint128 bond0 = hv.bankFull().bondShares;
        uint160 s0 = _sqrtSpot();
        _place(-940, 1060, _none(), 0);
        (, Holdings memory pb, Holdings memory pa, uint128 c0, uint128 r) = hv.pendingAction();
        Exp memory e = _expect(b, pa, s0, s0, tier.REF_BAND, S);
        Bank memory bk = hv.bankFull();
        assertEq(bk.reserveShares, e.burnR, "the reserve's shares");
        assertEq(bk.reserveBond, e.burnR, "all from the bond");
        assertEq(bk.bondShares, bond0 - e.burn - e.burnR, "the bond pays the charge and the reserve");
        vm.prank(bob);
        vm.expectRevert(VaultCommon.MarkoutPending.selector);
        IVaultEntries(address(hv)).deposit(1e18, type(uint256).max, type(uint256).max, bob);
        advance(2 + tier.H_MARKOUT / blocktime);
        (c0, r);
        (uint256 extra,, uint256 burnS) =
            _settleExtra(pb, pa, hv.markout(), tier.REF_BAND, tier.FEE_TICKS, hv.feePips(), bk.reserveShares);
        extra;
        hv.settle();
        bk = hv.bankFull();
        assertEq(bk.reserveShares, 0, "settled");
        assertEq(bk.bondShares, bond0 - e.burn - burnS, "the unburned reserve returns to the bond");
        _deposit(bob, 1e18);
    }

    function _setBond(uint256 n) internal {
        Bank memory bk = hv.bankFull();
        bk.vestedUnclaimed += uint128(bk.bondShares - n);
        bk.bondShares = uint128(n);
        hv.setBank(bk);
    }

    // ------------------------------------------------------------------ U-epilogue

    /// @notice The first placement re-bases `NAV₀` and `δ₀` at the live reference on the new holdings (control: junk
    ///         before); a second placement that leaves half the NAV idle halves `δ₀`, and the old position is removed.
    function test_U_epilogue__firstPlacementThenDeltaHalved() public {
        _setRef(20);
        hv.settle();
        Scalars memory s = hv.scalars();
        hv.setScalars(1, 1, s.tauV0, s.segStart, s.baseOwed, s.epoch, 0, 0);
        _place(-1000, 1000, _none(), 0);
        (uint256 nav, uint256 pos) = ExecLedger.value(hv.holdings(), _sqrt(20));
        uint256 d1 = FullMath.mulDiv(pos, WAD, nav);
        assertEq(hv.scalars().nav0, nav, "NAV0 at the live reference");
        assertEq(hv.scalars().delta0, d1, "delta0 at the live reference");
        assertGt(d1, 9e17, "nearly all deployed");
        advance(tier.COOLDOWN); // the markout settles in the next action's prologue
        _credit(0, nav * 4 / 5); // idle token1 ≈ 0.8·NAV: the next placement deploys ≈ 1/1.8 of NAV
        _place(-990, 1010, _none(), 0);
        assertEq(_positionL(-1000, 1000), 0, "the old position was removed");
        assertEq(_positionL(-990, 1010), hv.holdings().L, "the new position");
        (nav, pos) = ExecLedger.value(hv.holdings(), _sqrt(20));
        uint256 d2 = FullMath.mulDiv(pos, WAD, nav);
        assertEq(hv.scalars().delta0, d2, "delta0 re-based");
        assertLe(d2 * 10, d1 * 6, "delta roughly halved");
    }

    // ------------------------------------------------------------------ U-mode

    /// @notice `setPositions` moves BREACHED to NORMAL and sets `placedByCurator` (breaches still count); in EXITED it
    ///         reverts `BadMode`.
    function test_U_mode__setPositionsFromBreachedToNormal() public {
        _driveBreaches(1); // EMPTY counts as out of range
        hv.settle();
        assertEq(_mode(), 1, "BREACHED");
        assertFalse(hv.bankFull().placedByCurator, "not placed");
        _place(-1000, 1000, _none(), 0);
        assertEq(_mode(), 0, "NORMAL");
        assertTrue(hv.bankFull().placedByCurator, "placed by the curator");
        assertEq(hv.bankFull().breaches, 1, "breaches still count");
        _setRef(5000);
        _driveBreaches(2);
        _setRef(0);
        hv.settle();
        assertEq(_mode(), 2, "EXITED");
        advance(tier.COOLDOWN);
        _placeReverts(-1000, 1000, _none(), 0, Ladder.BadMode.selector);
    }

    /// @notice Pending removal: with the third breach latched but not processed, `setPositions` reverts `BadMode` after
    ///         the prologue (the call reverts, so the breach stays pending, and a later checkpoint reaches EXITED).
    function test_U_mode__setPositionsWithThePendingRemovalReverts() public {
        _driveBreaches(3);
        assertEq(_mode(), 0, "not processed yet");
        _placeReverts(-1000, 1000, _none(), 0, Ladder.BadMode.selector);
        assertEq(_obs().pendingBreaches, 3, "still pending");
        hv.settle();
        assertEq(_mode(), 2, "EXITED");
    }
}
