// SPDX-License-Identifier: MIT
pragma solidity 0.8.26;

import {console} from "forge-std/console.sol";
import {LiquidityValue} from "../../src/libraries/LiquidityValue.sol";
import {CellBase} from "./CellBase.sol";
import {PreSwap} from "../../src/types/PreSwap.sol";
import {ModifyLiquidityParams} from "@uniswap/v4-core/src/types/PoolOperation.sol";
import {SeedParams} from "../../src/interfaces/ITouchstoneFactory.sol";

/// @notice The Python cells' worst attack rows (G0, `internal` fixtures `n4`, `n4edge`, `adv3`, `m2d`, `m2dc`; the
///         largest expected net per cell), replayed through the real vault in the USDC/WETH-like setting (token0 at 6
///         decimals). Each row pins Python's state and configuration: the vault is seeded at a tick inside the row's old
///         range with the row's holdings scaled down by the bond-cap multiple (the first placement on the old range then
///         leaves the row's idle split), filled to the cap, and the market moved to the row's tick; then the row's push
///         target, new range, third-party depth, JIT range and size, swap side, amount and limit, or the reference's lag.
///         The rows pin configurations through the production path; the G0 differential was the bit-exact replay.
abstract contract PinnedRow is CellBase {
    /// @dev The row's old range, market tick and holdings (idle0, idle1, L), from the fixture.
    function _row() internal pure virtual returns (int24 oldLo, int24 r, uint256 idle0, uint256 idle1, uint128 L);

    function _highTick() internal pure override returns (bool) {
        return true;
    }

    /// @dev The seed (and placement) tick: inside the old range, nearest the row's market tick.
    function _seedTick() internal view override returns (int24) {
        (int24 lo, int24 r,,,) = _row();
        return r < lo + 10 ? lo + 10 : (r > lo + 1990 ? lo + 1990 : r);
    }

    function _firstLo() internal view override returns (int24) {
        (int24 lo,,,,) = _row();
        return lo;
    }

    /// @dev The row's holdings at the seed tick, divided by the bond-cap multiple (the fill to the cap restores them).
    function _seedParams() internal view override returns (SeedParams memory p) {
        (int24 lo,, uint256 i0, uint256 i1, uint128 L) = _row();
        (uint256 x, uint256 y) = LiquidityValue.amounts(L, lo, lo + 2000, _sqrt(_seedTick()));
        uint256 m = 574; // the cap multiple ⌊WAD / BOND_RATIO⌋ at the TEST tier
        p = SeedParams({a0: (x + i0) / m + 1, a1: (y + i1) / m + 1, curator: address(0), wit0: 1e6, wit1: 1e12});
    }

    function setUp() public virtual override {
        super.setUp();
        (, int24 r,,,) = _row();
        if (r != market) _moveMarket(r);
    }

    function _report(Out memory o, string memory name) internal {
        assertFalse(o.reverted, string.concat(name, ": the row acts"));
        assertGt(o.charge0, 0, string.concat(name, ": the row is charged"));
        _assertLoses(o, name);
        console.log(string.concat(name, " net (wei)"), o.net);
    }
}

/// @notice n4 row 1 (the market 112 ticks below the old range; push +20; the new range at 194,450). The same
///         configuration heads m2d's one-shot lane, replayed there at depths 0.3 and 3.
contract MPinnedN4Row1Test is PinnedRow {
    function _row() internal pure override returns (int24, int24, uint256, uint256, uint128) {
        return (195500, 195388, 4, 228376211849166782459, 431379106741247835);
    }

    function test_M_N4__pinnedPythonWorstRow1() public {
        _report(_attackPlace(_sqrt(195408), 194450, _none(), 0), "n4 #1");
    }

    /// @notice The same row at each of the five third-party depths (a loop: the domain has five values).
    function test_M_2d__pinnedPythonWorstOneShotRow() public {
        for (uint256 k; k < 5; ++k) {
            uint256 snap = vm.snapshotState();
            _thirdPartyDepth(KS[k]);
            _report(_attackPlace(_sqrt(195408), 194450, _none(), 0), "m2d #1");
            vm.revertToState(snap);
        }
    }
}

/// @notice n4 row 2 (40 ticks above the old range; push +20; the new range at 195,330).
contract MPinnedN4Row2Test is PinnedRow {
    function _row() internal pure override returns (int24, int24, uint256, uint256, uint128) {
        return (195250, 197290, 14559134179, 500000000332, 558870206120985806);
    }

    function test_M_N4__pinnedPythonWorstRow2() public {
        _report(_attackPlace(_sqrt(197310), 195330, _none(), 0), "n4 #2");
    }
}

/// @notice n4 row 3 (55 ticks above the old range; push −20; the new range at 195,380).
contract MPinnedN4Row3Test is PinnedRow {
    function _row() internal pure override returns (int24, int24, uint256, uint256, uint128) {
        return (195310, 197365, 25938607453, 500000001234, 554931328061870533);
    }

    function test_M_N4__pinnedPythonWorstRow3() public {
        _report(_attackPlace(_sqrt(197345), 195380, _none(), 0), "n4 #3");
    }
}

/// @notice n4edge rows 1 and 2 (in range; spot pushed one unit inside the lower edge 189,440 at Python's exact sqrt
///         target; the edge placement on it; row 2 with a third party holding the vault's liquidity on ±3,000 ticks).
contract MPinnedN4EdgeRow1Test is PinnedRow {
    function _row() internal pure override returns (int24, int24, uint256, uint256, uint128) {
        return (189310, 189440, 3029, 500000001027, 410744806733784628);
    }

    function test_M_N4edge__pinnedPythonWorstRow1() public {
        _report(_attackPlace(1028753316194410656463639939670940, 189440, _none(), 0), "n4edge #1");
    }

    function test_M_N4edge__pinnedPythonWorstRow2() public {
        _thirdPartyDepth(1e18);
        _report(_attackPlace(1028753316194410656463639939670940, 189440, _none(), 0), "n4edge #2");
    }
}

/// @notice n4edge row 3 (30 ticks below the old range; the edge target at 192,360).
contract MPinnedN4EdgeRow3Test is PinnedRow {
    function _row() internal pure override returns (int24, int24, uint256, uint256, uint128) {
        return (192390, 192360, 3052, 500000001031, 482687317120725690);
    }

    function test_M_N4edge__pinnedPythonWorstRow3() public {
        _report(_attackPlace(1190460726129085144809444156584795, 192360, _none(), 0), "n4edge #3");
    }
}

/// @notice adv3 row 1 (252 ticks below the old range; JIT 1e24 on [188,190, 188,210); the curator sells 16,388 USDC
///         of token0 with the limit 10 ticks adverse), as a `rebalanceSwap` and as a placement's pre-swap.
contract MPinnedADV3Row1Test is PinnedRow {
    function _row() internal pure override returns (int24, int24, uint256, uint256, uint128) {
        return (188460, 188208, 78039121743, 11632506349021590797, 405582375342551378);
    }

    /// @dev A max-liquidity placement never leaves idle in both tokens: an honest `rebalanceSwap` of 1% of NAV buys
    ///      the token0 the row sells (Python's state holds 78,039 USDC idle).
    function setUp() public override {
        super.setUp();
        uint256 amt = _nav(_sqrt(market)) / 100;
        // a third party's depth for the honest swap, withdrawn after it (the row itself has none)
        int24 c = _floor(market);
        lpRouter.modifyLiquidity(key, ModifyLiquidityParams(c - 3000, c + 3000, 1e20, 0), "");
        vm.prank(owner);
        bundler.rebalanceSwap(false, uint128(amt), type(uint256).max);
        lpRouter.modifyLiquidity(key, ModifyLiquidityParams(c - 3000, c + 3000, -1e20, 0), "");
        _thirdPartyPush(_sqrt(market));
        _settleDue();
        assertGt(_h().idle0, 16388214910, "idle token0 for the row");
    }

    function test_M_ADV3__pinnedPythonWorstRow1() public {
        _report(_attackSwap(0, 188190, 188210, 1e24, true, 16388214910), "adv3 #1");
    }

    function test_M_ADV3__pinnedPythonWorstRow1PreSwap() public {
        (jitLo, jitHi, jitL) = (188190, 188210, 1e24);
        // the inventory is mostly token0: the range sits with spot near its lower edge
        _report(_attackPlace(0, _floor(market) - 20, PreSwap(true, true, 16388214910), 0), "adv3 #1 pre-swap");
    }
}

/// @notice adv3 row 3 (in range; JIT 1e24 on [188,800, 188,820); the curator buys token0 with 2.584 WETH).
contract MPinnedADV3Row3Test is PinnedRow {
    function _row() internal pure override returns (int24, int24, uint256, uint256, uint128) {
        return (188460, 188801, 77804764542, 12306073139723034147, 405582375342551378);
    }

    function test_M_ADV3__pinnedPythonWorstRow3() public {
        _report(_attackSwap(0, 188800, 188820, 1e24, false, 2584275254341113635), "adv3 #3");
    }
}

/// @notice m2dc's third row by expected net (every m2dc fixture row is an exactness row, class 3; rows 1 and 2 are
///         pinned below): the market at 197,400 in the old range, the reference lagging at 197,404 (with 197,416 and
///         197,424 as extra lags), the placement on [197,400, 199,400) with spot pushed one unit above its lower edge,
///         the markout at the market.
contract MPinnedM2dcTest is PinnedRow {
    function _row() internal pure override returns (int24, int24, uint256, uint256, uint128) {
        return (196560, 197400, 2362, 967875153375, 460160134679503719);
    }

    function test_M_2d__pinnedPythonWorstLagRows() public {
        int24[3] memory refs = [int24(197404), 197416, 197424];
        for (uint256 i; i < 3; ++i) {
            uint256 snap = vm.snapshotState();
            _lagReference(refs[i]);
            _report(_attackPlace(1531622755499097659974717477634357, 197400, _none(), 0), "m2dc lag row");
            vm.revertToState(snap);
        }
    }

    /// @dev The reference reads `ref` over the whole TWAP window up to now, then the market from the next block (the
    ///      markout window reads the market).
    function _lagReference(int24 ref) internal {
        uint32[] memory st = new uint32[](2);
        int24[] memory tk = new int24[](2);
        (st[0], tk[0]) = (uint32(vm.getBlockTimestamp() - 2 hours), ref);
        (st[1], tk[1]) = (uint32(vm.getBlockTimestamp() + 1), market);
        refPool.setPath(st, tk);
    }
}

/// @notice m2dc's rows 1 and 2 by expected net (fix review S14-05): the market just above the old range's upper edge,
///         the reference lagging below it; Python's op sequence: a push to the row's `u` (above the market), the
///         curator's placement whose own move takes spot down to one unit inside the old range's upper edge, on the range ending at
///         that edge, then the unwind to the market and the markout there.
abstract contract PinnedM2dcEdgeRow is PinnedRow {
    /// @dev The row's (refTick, u, the move's target tick = the new range's upper edge).
    function _ops() internal pure virtual returns (int24 ref, int24 u, int24 edge);

    function _pinnedEdgeRow(string memory name) internal {
        (int24 ref, int24 u, int24 edge) = _ops();
        uint32[] memory st = new uint32[](2);
        int24[] memory tk = new int24[](2);
        (st[0], tk[0]) = (uint32(vm.getBlockTimestamp() - 2 hours), ref);
        (st[1], tk[1]) = (uint32(vm.getBlockTimestamp() + 1), market);
        refPool.setPath(st, tk);
        // the move stops one unit inside the new range's upper edge (Python's exact `s_spot`)
        _report(_attackPlace(_sqrt(u), edge - W, _none(), _sqrt(edge) - 1), name);
    }
}

/// @notice m2dc row 1: old range [191,720, 193,720), market 193,722, reference 193,706, u 193,723, move to 193,710,
///         range [191,710, 193,710); Python's expected net −258,842,723.
contract MPinnedM2dcRow1Test is PinnedM2dcEdgeRow {
    function _row() internal pure override returns (int24, int24, uint256, uint256, uint128) {
        return (191720, 193722, 3050, 500000000983, 466562211038170673);
    }

    function _ops() internal pure override returns (int24, int24, int24) {
        return (193706, 193723, 193710);
    }

    function test_M_2d__pinnedPythonM2dcRow1() public {
        _pinnedEdgeRow("m2dc #1");
    }
}

/// @notice m2dc row 2: old range [194,110, 196,110), market 196,135, reference 196,109, u 196,140, move to 196,110,
///         range [194,110, 196,110); Python's expected net −329,477,665.
contract MPinnedM2dcRow2Test is PinnedM2dcEdgeRow {
    function _row() internal pure override returns (int24, int24, uint256, uint256, uint128) {
        return (194110, 196135, 3061, 500000000497, 527613821670171886);
    }

    function _ops() internal pure override returns (int24, int24, int24) {
        return (196109, 196140, 196110);
    }

    function test_M_2d__pinnedPythonM2dcRow2() public {
        _pinnedEdgeRow("m2dc #2");
    }
}

