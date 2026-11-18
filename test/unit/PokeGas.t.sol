// SPDX-License-Identifier: MIT
pragma solidity 0.8.26;

import {console} from "forge-std/console.sol";
import {LiquidPoolBase} from "../utils/LiquidPoolBase.sol";
import {Bank} from "../../src/types/Bank.sol";
import {PreSwap} from "../../src/types/PreSwap.sol";
import {Ladder} from "../../src/libraries/Ladder.sol";
import {IVaultEntries} from "../../src/interfaces/ITouchstoneVaultExtension.sol";
import {EpochRecord} from "../../src/types/EpochRecord.sol";

/// @notice G-3 (spec §9.1): `poke` costs at most 700k gas on the union of its heavy legs, measured in two scenarios that
///         cannot share one block (a vest needs the reference in range, a breach needs it out): (a) a due settlement, a
///         matured cohort vested through the gate's live read, base minted, a roll with a gap record and the witness
///         roll; (b) a due settlement, three latched breaches processed (forfeit, drip, `ackBreaches`, the removal), a
///         silent gap freezing all 8 grid slots, a roll with a gap record and the witness roll. Measured on the
///         factory-deployed harness (the production path plus test seams: an upper bound), every touched account
///         cooled first.
contract PokeGasTest is LiquidPoolBase {
    uint256 internal constant G3_MAX = 700_000;
    /// @dev The transaction's intrinsic gas (`poke()` has a 4-byte calldata), which `gasleft()` around the call misses.
    uint256 internal constant INTRINSIC = 21_000 + 16 * 4;

    function _placeAndPend() internal {
        refPool.setTick(0);
        hv.placeForTest(-1800, 200);
        advance(tier.COOLDOWN);
        vm.prank(curator);
        hv.setPositions(-1800, 200, PreSwap(false, false, 0), 0, type(uint256).max); // δ₀ > 0: base mints
        _keepAlive(tier.H_MARKOUT + 20);
        hv.settle();
        advance(tier.COOLDOWN);
        vm.prank(curator);
        hv.rebalanceSwap(true, 1e16, type(uint256).max); // pending; its window closes before the measured poke
    }

    function _cool() internal {
        vm.cool(address(hv));
        vm.cool(address(hook));
        vm.cool(address(manager));
        vm.cool(address(refPool));
        vm.cool(address(token0));
        vm.cool(address(token1));
    }

    function _measure(string memory name) internal returns (uint256 used) {
        _cool();
        uint256 g = gasleft();
        IVaultEntries(address(hv)).poke();
        used = g - gasleft() + INTRINSIC;
        console.log(name, used);
        if (vm.envOr("TS_MUTATION_RUN", uint256(0)) == 0) vm.snapshotValue("TouchstoneVault", name, used);
    }

    /// @notice (a) the vest-heavy poke.
    function test_G_3__pokeWithVestMintSettlementAndGapRollWithinTheBudget() public {
        _placeAndPend();
        Bank memory b = hv.bankFull();
        b.maturedPending += 1e20; // a matured cohort to vest through the gate
        b.bondShares -= 1e20;
        hv.setBank(b);
        uint32 e0 = hv.scalars().epoch;
        _keepAlive(3 * tier.EPOCH); // observed (no gap), the vault untouched: the poke rolls over skipped epochs
        assertGt(_obs().epoch, e0 + 1, "precondition: epochs skipped");
        uint256 used = _measure("G-3 poke (vest, mint, settle, gap roll, witness)");
        (uint40 tA,,,,) = hv.pendingAction();
        assertEq(tA, 0, "precondition: the action settled");
        assertEq(hv.bankFull().maturedPending, 0, "precondition: the matured pay vested");
        assertGt(hv.record(e0).end, 0, "precondition: the record closed");
        assertGt(hv.record(e0 + 1).end, 0, "precondition: a gap record");
        assertLe(used, G3_MAX, "G-3: poke within 700k");
    }

    /// @notice (b) the breach-heavy poke.
    function test_G_3__pokeWithThreeBreachesFreezeSettlementAndGapRollWithinTheBudget() public {
        _placeAndPend();
        refPool.setTick(3000); // the reference leaves the range
        _driveBreaches(3);
        uint32 e0 = hv.scalars().epoch;
        advance(9 * tier.GRID + 2 * tier.EPOCH); // silent: every grid slot freezes at the poke
        uint256 used = _measure("G-3 poke (3 breaches, freeze, settle, gap roll, witness)");
        Bank memory b = hv.bankFull();
        assertEq(b.mode, Ladder.EXITED, "precondition: the removal processed");
        (uint40 tA,,,,) = hv.pendingAction();
        assertEq(tA, 0, "precondition: the action settled");
        assertGt(hv.record(e0 + 1).end, 0, "precondition: a gap record");
        assertLe(used, G3_MAX, "G-3: poke within 700k");
    }
}
