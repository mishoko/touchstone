// SPDX-License-Identifier: MIT
pragma solidity 0.8.26;

import {console} from "forge-std/console.sol";
import {FullMath} from "@uniswap/v4-core/src/libraries/FullMath.sol";
import {TickMath} from "@uniswap/v4-core/src/libraries/TickMath.sol";
import {LiquidPoolBase} from "../utils/LiquidPoolBase.sol";
import {ExecLedger} from "../../src/libraries/ExecLedger.sol";
import {LiquidityValue} from "../../src/libraries/LiquidityValue.sol";
import {Bank} from "../../src/types/Bank.sol";
import {Holdings} from "../../src/types/Holdings.sol";
import {PreSwap} from "../../src/types/PreSwap.sol";

/// @notice M-10 (spec §5.1 T16, clawback sniping; the defence: a checkpoint on every supply change). The factory-
///         deployed harness at the tier's real m, market and reference at tick 0 (token0 and token1 valued 1:1).
///         - Lane A, the forfeiture: a breach is latched in the hook and not yet processed, with the curator's unvested
///           pay forfeitable; a sniper deposits (its checkpoint processes the breach first, so the forfeit enters the
///           drip pool before the sniper's shares exist) and withdraws in the same block. Grid: 1, 5, 10 and 50% of
///           the supply. Pass: the sniper's P&L ≤ 0. Detection control: the same sniper held through the drip gains
///           > 0 and at most its supply share of the forfeit (the time-weighted share).
///         - Lane B, the base mint: observed in-range time has accrued base that no one has minted; a holder withdraws
///           (its checkpoint mints the base first). Pass: the payout is the post-mint pro-rata share. Positive control:
///           the payout without the checkpoint (pre-mint pro-rata) exceeds it, so the escape the defence closes is real.
contract M10Test is LiquidPoolBase {
    uint256 internal constant WAD = 1e18;

    function _setRef(int24 t) internal {
        uint32[] memory s = new uint32[](1);
        int24[] memory k = new int24[](1);
        (s[0], k[0]) = (1, t);
        refPool.setPath(s, k);
    }

    function setUp() public override {
        super.setUp();
        _setRef(0);
    }

    /// @notice Tokens at the market (tick 0: 1 token0 = 1 token1).
    function _val(uint256 a0, uint256 a1) internal pure returns (int256) {
        return int256(a0 + a1);
    }

    /// @notice A pending breach over a forfeitable matured cohort of `pay` shares (moved from the bond).
    function _pendingForfeit(uint128 pay) internal {
        Bank memory b = hv.bankFull();
        b.maturedPending += pay;
        b.bondShares -= pay;
        hv.setBank(b);
        _driveBreaches(1);
        assertGt(_obs().pendingBreaches, 0, "a breach is latched, unprocessed");
        assertEq(hv.bankFull().maturedPending, pay, "nothing forfeited yet");
    }

    function _snipe(uint256 shares, uint256 holdBlocks) internal returns (int256 pnl) {
        _fundUser(bob, 1e36);
        (uint256 i0, uint256 i1) = _deposit(bob, shares);
        assertEq(hv.bankFull().maturedPending, 0, "the deposit's checkpoint forfeited first");
        assertGt(hv.bankFull().forfeitPool, 0, "into the drip pool");
        if (holdBlocks > 0) _keepAlive(holdBlocks);
        (uint256 o0, uint256 o1) = _withdraw(bob, hv.balanceOf(bob));
        pnl = _val(o0, o1) - _val(i0, i1);
    }

    function test_M_10__forfeitSnipeInTheSameBlockLoses() public {
        uint16[4] memory pct = [uint16(1), 5, 10, 50];
        for (uint256 i; i < 4; ++i) {
            uint256 snap = vm.snapshotState();
            _pendingForfeit(5e23);
            uint256 shares = hv.totalSupply() * pct[i] / 100;
            int256 pnl = _snipe(shares, 0);
            console.log("M-10 lane A same-block pct", pct[i]);
            console.logInt(pnl);
            assertLe(pnl, 0, "the sniper does not profit");
            vm.revertToState(snap);
        }
    }

    function test_M_10__ctlSnipeHeldThroughTheDripGainsItsTimeWeightedShare() public {
        _pendingForfeit(5e23);
        uint256 S0 = hv.totalSupply();
        uint256 shares = S0 / 10;
        uint256 navPre = _navNow();
        int256 pnl = _snipe(shares, tier.FORFEIT_DRIP_BLOCKS + 20);
        console.log("M-10 control, held through the drip");
        console.logInt(pnl);
        assertGt(pnl, 0, "control: the forfeit reaches holders and the measure sees it");
        // the most a holder of `shares` can gain from the whole drip: its shares re-priced from the post-deposit
        // supply to that supply less the forfeit (deposits are pro rata, so NAV scales with the supply)
        uint256 nav1 = FullMath.mulDiv(navPre, S0 + shares, S0);
        uint256 bound = FullMath.mulDiv(shares, nav1, S0 + shares - 5e23) - FullMath.mulDiv(shares, nav1, S0 + shares);
        assertLe(uint256(pnl), bound + 10, "at most its time-weighted share of the forfeit");
    }

    function _navNow() internal view returns (uint256 nav) {
        (nav,) = ExecLedger.value(hv.holdings(), TickMath.getSqrtPriceAtTick(0));
    }

    /// @notice Lane B: a holder's withdrawal while base is accrued and unminted pays the post-mint pro-rata share.
    function test_M_10__withdrawBeforeAPendingBaseMintCannotEscapeIt() public {
        hv.placeForTest(-1800, 200);
        advance(tier.COOLDOWN);
        vm.prank(curator);
        hv.setPositions(-1800, 200, PreSwap(false, false, 0), 0, type(uint256).max); // delta0 > 0
        _keepAlive(tier.H_MARKOUT + 20);
        hv.settle();
        _keepAlive(200); // observed in-range time: base accrued, not minted
        uint16[4] memory pct = [uint16(1), 5, 10, 50];
        for (uint256 i; i < 4; ++i) {
            uint256 snap = vm.snapshotState();
            uint256 S = hv.totalSupply();
            uint256 out = hv.balanceOf(alice) * pct[i] / 100;
            uint256 navPre = _navNow(); // the in-kind payout is pro rata on holdings; at tick 0 value = amounts
            int256 preMint = int256(FullMath.mulDiv(navPre, out, S)); // the payout without the checkpoint
            (uint256 o0, uint256 o1) = _withdraw(alice, out);
            uint256 S1 = hv.totalSupply() + out; // the supply the withdrawal priced against (after the mint)
            assertGt(S1, S, "the checkpoint minted base before the burn");
            int256 got = _val(o0, o1);
            console.log("M-10 lane B pct", pct[i]);
            console.logInt(preMint - got);
            assertLe(got, int256(FullMath.mulDiv(navPre, out, S1)) + 2, "post-mint pro-rata");
            assertGt(preMint, got, "control: without the checkpoint the holder would escape the mint");
            vm.revertToState(snap);
        }
    }
}
