// SPDX-License-Identifier: MIT
pragma solidity 0.8.26;

import {console} from "forge-std/console.sol";
import {Vm} from "forge-std/Vm.sol";
import {FullMath} from "@uniswap/v4-core/src/libraries/FullMath.sol";
import {TickMath} from "@uniswap/v4-core/src/libraries/TickMath.sol";
import {LiquidPoolBase} from "../utils/LiquidPoolBase.sol";
import {Scalars} from "../utils/VaultHarness.sol";
import {MockRefPool} from "../utils/MockRefPool.sol";
import {ITouchstoneHook} from "../../src/interfaces/ITouchstoneHook.sol";
import {IVaultEntries} from "../../src/interfaces/ITouchstoneVaultExtension.sol";
import {ExecLedger} from "../../src/libraries/ExecLedger.sol";
import {VaultCommon} from "../../src/TouchstoneVaultBase.sol";
import {PreSwap} from "../../src/types/PreSwap.sol";

/// @notice M-N2 (spec §5.1 N2, §3.5 segmentation; fix review S16B "What M-N2 must cover"): forced checkpoints cannot
///         escape or inflate the base. On one reference schedule (the hook observes every 10 blocks, the reference in
///         range), a run with a single checkpoint at the end and runs with a checkpoint forced every 10 blocks, with a
///         deferral across an epoch boundary (a gas-burning reference makes every read fail while the cache is stale),
///         and with deposits and withdrawals in between, are compared:
///         - the base AMOUNT (Σ of the `Checkpoint` events' `base`) is independent of segmentation: forced
///           checkpoints never raise it (the floors only lose) and lose at most 1 wei per segment;
///         - a deferred amount is minted once, at the next priced checkpoint (`baseOwed` back to 0);
///         - the shares minted are worth the base amount at the mint price, so a mint timed for a low NAV per share
///           gains nothing (value of the minted shares ≤ amount + 1 at two reference prices);
///         - NAV₀ re-bases with the supply on a deposit or withdrawal (the base per unit of NAV is unchanged; reported).
///         Positive control (power): one more in-range block moves the amount by more than the per-segment bound.
contract MN2Test is LiquidPoolBase {
    uint256 internal constant WAD = 1e18;
    int24 internal constant LO = -1800;
    int24 internal constant HI = 200;
    bytes32 internal constant T_CHECKPOINT = keccak256("Checkpoint(uint64,uint64,uint256,uint64,uint256,uint128,int24,int24)");

    /// @dev The reference pool's tick from now on (its TWAP follows over `REF_WINDOW`).
    function _setRef(int24 t) internal {
        refPool.setTick(t);
    }

    function _ve() internal view returns (IVaultEntries) {
        return IVaultEntries(vault);
    }

    function setUp() public override {
        super.setUp();
        _setRef(0);
        hv.placeForTest(LO, HI);
        advance(tier.COOLDOWN);
        vm.prank(curator);
        hv.setPositions(LO, HI, PreSwap(false, false, 0), 0, type(uint256).max);
        _keepAlive(tier.H_MARKOUT + 20);
        hv.settle();
        assertGt(hv.scalars().delta0, 0, "precondition: a real placement");
    }

    function _sumBase(Vm.Log[] memory logs) internal view returns (uint256 sum, uint256 segs) {
        for (uint256 i; i < logs.length; ++i) {
            if (logs[i].emitter == vault && logs[i].topics[0] == T_CHECKPOINT) {
                (,,,, uint256 base,,,) =
                    abi.decode(logs[i].data, (uint64, uint64, uint256, uint64, uint256, uint128, int24, int24));
                sum += base;
                ++segs;
            }
        }
    }

    /// @dev `mode` 0: the hook observes, one checkpoint at the end; 1: a checkpoint every 10 blocks; 2: as 1, with
    ///      every reference read failing (a gas-burning reference) over steps 8-13, across an epoch boundary, so
    ///      checkpoints defer; 3: as 0 with the same failing reads (the hook-only run on that schedule). Steps of 10
    ///      blocks; `n` steps.
    function _run(uint256 mode, uint256 n) internal returns (uint256 amount, uint256 segs) {
        vm.recordLogs();
        for (uint256 i; i < n; ++i) {
            advance(10);
            bool starve = mode >= 2 && i >= 8 && i < 14; // spans an epoch boundary (EPOCH = 120 = 12 steps)
            if (starve) refPool.setMode(MockRefPool.Mode.GasBomb);
            else refPool.setMode(MockRefPool.Mode.Ok);
            if (mode == 0 || mode == 3) ITouchstoneHook(hook).refreshRef();
            else _ve().poke();
        }
        refPool.setMode(MockRefPool.Mode.Ok);
        advance(1);
        _ve().poke();
        Vm.Log[] memory logs = vm.getRecordedLogs();
        (amount, segs) = _sumBase(logs);
        if (mode == 2) {
            uint256 deferrals;
            for (uint256 i; i < logs.length; ++i) {
                if (logs[i].emitter == vault && logs[i].topics[0] == VaultCommon.CheckpointDeferred.selector) ++deferrals;
            }
            assertGt(deferrals, 0, "precondition: the starved run deferred");
        }
        assertEq(hv.scalars().baseOwed, 0, "every deferred amount minted");
    }

    function test_M_N2__forcedCheckpointsNeitherRaiseNorEscapeTheBase() public {
        uint256 n = 30;
        // align: the starved steps 8-13 (blocks +80..+140) span an epoch boundary
        advance(tier.EPOCH - (block.number % tier.EPOCH) + tier.EPOCH - 100);
        assertLt((block.number + 80) / tier.EPOCH, (block.number + 140) / tier.EPOCH, "precondition: the crossing");
        uint256 snap = vm.snapshotState();
        (uint256 a,) = _run(0, n);
        vm.revertToState(snap);
        (uint256 b, uint256 sb) = _run(1, n);
        vm.revertToState(snap);
        // the deferral run: some reads fail while the cache is stale; the amount is the same up to the floors
        (uint256 c, uint256 sc) = _run(2, n);
        vm.revertToState(snap);
        console.log("M-N2 base: single, forced, deferred", a, b, c);
        assertGt(a, 0, "precondition: base accrued");
        assertLe(b, a, "forced checkpoints never raise the amount");
        assertLe(a - b, sb, "and lose at most 1 wei per segment");
        // the deferral run against the hook-only run on the same failing reads (both credit only observed time)
        (uint256 d,) = _run(3, n);
        vm.revertToState(snap);
        console.log("M-N2 base: hook-only on the failing schedule", d);
        assertLe(c, d, "a deferral never raises the amount");
        assertLe(d - c, sc, "and loses at most 1 wei per segment of base (the minting of baseOwed: the next test)");
        // control (power): one more in-range step moves the amount by far more than the per-segment bound
        (uint256 a2,) = _run(0, n + 1);
        vm.revertToState(snap);
        assertGt(a2 - a, 10 * sb, "control: the measure resolves one step of base");
    }

    /// @notice The deferred run across the epoch boundary emits `CheckpointDeferred` and leaves no `baseOwed`.
    function test_M_N2__deferralAcrossAnEpochBoundaryIsMintedOnce() public {
        _keepAlive(40);
        // stale cache (silence past 2·GRID), failing reads, across an epoch boundary
        refPool.setMode(MockRefPool.Mode.GasBomb);
        advance(2 * tier.GRID + 1);
        uint32 e0 = uint32(block.number / tier.EPOCH);
        advance(tier.EPOCH - (block.number % tier.EPOCH) + 5);
        assertGt(block.number / tier.EPOCH, e0, "precondition: across a boundary");
        vm.recordLogs();
        _ve().poke();
        Vm.Log[] memory logs = vm.getRecordedLogs();
        bool deferred;
        for (uint256 i; i < logs.length; ++i) {
            if (logs[i].emitter == vault && logs[i].topics[0] == VaultCommon.CheckpointDeferred.selector) deferred = true;
        }
        assertTrue(deferred, "deferred");
        uint256 owed = hv.scalars().baseOwed;
        assertGt(owed, 0, "owed");
        refPool.setMode(MockRefPool.Mode.Ok);
        advance(1);
        uint256 S = hv.totalSupply();
        (uint256 navPre,) = ExecLedger.value(hv.holdings(), TickMath.getSqrtPriceAtTick(0));
        _ve().poke();
        assertEq(hv.scalars().baseOwed, 0, "minted once");
        uint256 minted = hv.totalSupply() - S;
        assertGt(minted, 0, "minted at the priced checkpoint");
        // the minted shares are worth the amount (plus the step's own base) at the mint price, not more
        (uint256 navPost,) = ExecLedger.value(hv.holdings(), TickMath.getSqrtPriceAtTick(0));
        assertEq(navPost, navPre, "a mint moves no assets");
        uint256 worth = FullMath.mulDiv(minted, navPost, hv.totalSupply());
        assertGe(worth + 1, owed, "the owed amount is minted in full");
    }

    /// @notice A mint timed for a low NAV per share (spec §3.5): the amount becomes shares at the minting checkpoint's
    ///         price, worth the amount there; valued later at another price q, the minted shares are worth
    ///         `amount·NAV(q)/NAV(p)`, the vault's own return on the base amount over the interval, exactly what any
    ///         holder of that value earns (disclosed in §3.5; second order: m × the segment × the move). Measured: the
    ///         base minted with the reference at -1500 (a low NAV in token1) is worth, at the reference 0, exactly
    ///         that (± 2 wei). Control: the timing gain over minting at 0 is visibly > 0 (the measure sees it).
    function test_M_N2__aMintAtALowNavPerShareEarnsOnlyTheVaultsReturnOnTheBase() public {
        _setRef(-1500);
        _keepAlive(200);
        uint256 S = hv.totalSupply();
        vm.recordLogs();
        _ve().poke();
        (uint256 amount,) = _sumBase(vm.getRecordedLogs());
        assertEq(hv.scalars().baseOwed, 0, "priced");
        assertGt(amount, 0, "base accrued");
        uint256 minted = hv.totalSupply() - S;
        (uint256 navLow,) = ExecLedger.value(hv.holdings(), TickMath.getSqrtPriceAtTick(_obs().refTick));
        (uint256 nav0,) = ExecLedger.value(hv.holdings(), TickMath.getSqrtPriceAtTick(0));
        uint256 S2 = hv.totalSupply();
        assertLe(FullMath.mulDiv(minted, navLow, S2), amount, "worth the amount at the mint price");
        uint256 worthAt0 = FullMath.mulDiv(minted, nav0, S2);
        uint256 expected = FullMath.mulDiv(amount, nav0, navLow); // the vault's return on the amount
        console.log("M-N2 mint at -1500: amount, worth at 0, vault return on it", amount, worthAt0, expected);
        assertLe(worthAt0, expected + 2, "no more than the vault's return on the base amount");
        assertGe(worthAt0 + 2, expected, "and exactly it, less rounding");
        assertGt(worthAt0, amount + 10, "control: the timing gain is visible to the measure");
    }

    /// @notice NAV₀ re-bases with the supply on deposits and withdrawals (no price read): the base per unit of NAV₀
    ///         per in-range block is unchanged by the flows (reported; spec §3.5).
    function test_M_N2__flowsRebaseNav0ProRata() public {
        uint256 nav0 = hv.scalars().nav0;
        uint256 s = hv.totalSupply();
        _keepAlive(60); // base accrues: the flows' prologues mint before the re-base
        _deposit(bob, s);
        uint256 S2 = hv.totalSupply();
        assertGt(S2 - s, s, "precondition: the deposit's prologue minted base (the supply moved)");
        assertEq(hv.scalars().nav0, FullMath.mulDiv(nav0, S2, S2 - s), "a deposit re-bases NAV0 with the supply");
        assertLt(uint256(hv.scalars().nav0) * (S2 - s), nav0 * S2, "floored strictly below the exact quotient (MU-317)");
        uint256 nav1 = hv.scalars().nav0;
        _keepAlive(60);
        uint256 sPre = hv.totalSupply();
        _withdraw(bob, s / 2);
        S2 = hv.totalSupply();
        assertGt(S2 + s / 2, sPre, "precondition: the withdrawal's prologue minted base (the supply moved)");
        assertEq(hv.scalars().nav0, FullMath.mulDiv(nav1, S2, S2 + s / 2), "a withdrawal re-bases it down");
        assertLt(uint256(hv.scalars().nav0) * (S2 + s / 2), nav1 * S2, "floored strictly below the exact quotient (MU-317)");
        console.log("M-N2 NAV0 before, after flows", nav0, hv.scalars().nav0);
    }
}
