// SPDX-License-Identifier: MIT
pragma solidity 0.8.26;

import {console} from "forge-std/console.sol";
import {FullMath} from "@uniswap/v4-core/src/libraries/FullMath.sol";
import {TickMath} from "@uniswap/v4-core/src/libraries/TickMath.sol";
import {LiquidPoolBase} from "../utils/LiquidPoolBase.sol";
import {IVaultEntries} from "../../src/interfaces/ITouchstoneVaultExtension.sol";
import {ExecLedger} from "../../src/libraries/ExecLedger.sol";
import {Bank} from "../../src/types/Bank.sol";

/// @notice M-snipe-forfeit (spec §5.1 R2-02; §3.7 the forfeiture drip): a sniper deposits before a predictable
///         forfeit and withdraws k blocks later. Two lanes: a latched breach (processed by the sniper's own deposit)
///         and a retire (the curator chooses the block, after a `topUpBond` that raises the deposit cap). Drip
///         lengths of the TEST tier (120 blocks) and the DEMO tier (3,600), set through `Bank.dripBlocks`, which is
///         where the vault reads it. Pass: the sniper's gain is at most its time-weighted share, its shares re-priced
///         over the part of the forfeit burned while it held them (`forfeit·min(k, D)/D`, rounded up). Positive
///         control: with an instant burn (`dripBlocks = 0`) the same-block snipe profits (> 0, above its bound of 0).
///         Market and reference at tick 0 (token0 and token1 valued 1:1).
contract MSnipeForfeitTest is LiquidPoolBase {
    uint128 internal constant PAY = 5e23;

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

    function _ve() internal view returns (IVaultEntries) {
        return IVaultEntries(vault);
    }

    function _navNow() internal view returns (uint256 nav) {
        (nav,) = ExecLedger.value(hv.holdings(), TickMath.getSqrtPriceAtTick(0));
    }

    /// @dev `PAY` shares of forfeitable pay in maturedPending (from the bond), the drip length `drip`.
    function _arm(uint40 drip) internal {
        Bank memory b = hv.bankFull();
        b.dripBlocks = drip;
        b.maturedPending += PAY;
        b.bondShares -= PAY;
        hv.setBank(b);
    }

    /// @dev The forfeit happens in the block of the sniper's deposit: `retireLane` = the curator retires right after
    ///      it; otherwise a latched breach is processed by the deposit's own checkpoint. Returns the sniper's P&L and
    ///      its time-weighted bound.
    function _snipe(bool retireLane, uint256 pct, uint256 hold, uint40 drip) internal returns (int256 pnl, uint256 bound) {
        _arm(drip);
        if (retireLane) {
            // the curator raises the bond (and the deposit cap) with its own shares before the deposit
            _fundUser(curator, 1e36);
            _deposit(curator, hv.totalSupply() / 100);
            uint256 cs = hv.balanceOf(curator);
            vm.prank(curator);
            _ve().topUpBond(cs);
        } else {
            _driveBreaches(1);
        }
        uint256 S0 = hv.totalSupply();
        uint256 shares = S0 * pct / 100;
        uint256 navPre = _navNow();
        _fundUser(bob, 1e36);
        (uint256 i0, uint256 i1) = _deposit(bob, shares);
        if (retireLane) {
            vm.prank(curator);
            _ve().retire();
        }
        assertEq(hv.bankFull().maturedPending, 0, "forfeited in the deposit's block");
        if (hold > 0) _keepAlive(hold);
        (uint256 o0, uint256 o1) = _withdraw(bob, hv.balanceOf(bob));
        pnl = int256(o0 + o1) - int256(i0 + i1);
        // the bound: the sniper's shares re-priced over the burned part of the forfeit while it held them (the
        // keep-alive rounds the hold up to whole steps of 10 blocks)
        uint256 held = (hold + 9) / 10 * 10;
        uint256 burned = drip == 0 ? PAY : FullMath.mulDivRoundingUp(PAY, held < drip ? held : drip, drip);
        uint256 S1 = S0 + shares;
        uint256 nav1 = FullMath.mulDiv(navPre, S1, S0);
        bound = FullMath.mulDiv(shares, nav1, S1 - burned) - FullMath.mulDiv(shares, nav1, S1);
    }

    function _grid(bool retireLane) internal {
        uint40[2] memory drips = [uint40(tier.FORFEIT_DRIP_BLOCKS), uint40(3600)]; // TEST, DEMO
        uint16[3] memory pct = [uint16(1), 10, 50];
        for (uint256 d; d < 2; ++d) {
            uint256[4] memory holds = [uint256(0), drips[d] / 4, drips[d] / 2, uint256(drips[d]) + 20];
            for (uint256 p; p < 3; ++p) {
                for (uint256 h; h < 4; ++h) {
                    uint256 snap = vm.snapshotState();
                    (int256 pnl, uint256 bound) = _snipe(retireLane, pct[p], holds[h], drips[d]);
                    console.log(retireLane ? "retire" : "breach", drips[d], pct[p], holds[h]);
                    console.logInt(pnl);
                    assertLe(pnl, int256(bound) + 10, "the sniper gains at most its time-weighted share");
                    vm.revertToState(snap);
                }
            }
        }
    }

    function test_M_snipe_forfeit__breachLaneGainsAtMostTheTimeWeightedShare() public {
        _grid(false);
    }

    function test_M_snipe_forfeit__retireLaneGainsAtMostTheTimeWeightedShare() public {
        _grid(true);
    }

    /// @notice Positive control, both lanes: with an instant burn the same-block snipe profits.
    function test_M_snipe_forfeit__ctlInstantBurnProfits() public {
        for (uint256 lane; lane < 2; ++lane) {
            uint256 snap = vm.snapshotState();
            (int256 pnl,) = _snipe(lane == 1, 10, 0, 0);
            console.log("control, instant burn, lane", lane);
            console.logInt(pnl);
            assertGt(pnl, 0, "control: an instant burn pays the same-block sniper");
            vm.revertToState(snap);
        }
    }
}
