// SPDX-License-Identifier: MIT
pragma solidity 0.8.26;

import {StateLibrary} from "@uniswap/v4-core/src/libraries/StateLibrary.sol";
import {TickMath} from "@uniswap/v4-core/src/libraries/TickMath.sol";
import {FullMath} from "@uniswap/v4-core/src/libraries/FullMath.sol";
import {FixedPoint128} from "@uniswap/v4-core/src/libraries/FixedPoint128.sol";
import {LiquidPoolBase} from "../utils/LiquidPoolBase.sol";
import {ITouchstoneHook} from "../../src/interfaces/ITouchstoneHook.sol";
import {IVaultEntries} from "../../src/interfaces/ITouchstoneVaultExtension.sol";
import {Holdings} from "../../src/types/Holdings.sol";
import {PreSwap} from "../../src/types/PreSwap.sol";
import {EpochRecord} from "../../src/types/EpochRecord.sol";
import {ExecLedger} from "../../src/libraries/ExecLedger.sol";

/// @notice E0's data run (spec §3.8, §10): a vault and a full-size passive simulation in one pool, epoch by epoch. The
///         curator re-places once per epoch, right after the epoch's poke has rolled the witness onto the passive-mandate
///         range: on that range (`same`) or `OFFSET` ticks from it (`offset`). Third parties trade a random walk and
///         the reference follows the pool. The passive leg is the vault's post-action holdings with its liquidity moved
///         frictionlessly onto the mandate range (the witness's range): its value at the reference plus the fees the
///         same liquidity earns there (the pool's fee growth inside the witness's range, a real position). Per epoch the
///         run writes the vault's NAV at the reference after the action and at the end, the action's loss, the passive
///         leg's change, the record (§3.9) and the witness's fees; `eval/E0.py` computes the ledger closure.
///         Run: `TS_E0_OUT=output/e0 forge test --mc E0DataTest` (skipped without `TS_E0_OUT`).
contract E0DataTest is LiquidPoolBase {
    using StateLibrary for *;

    uint256 internal constant EPOCHS = 8;
    int24 internal constant OFFSET = 200;
    uint256 internal constant WAD = 1e18;

    struct Ep {
        uint32 e;
        uint256 navAfter; // the vault's NAV at the reference after the action
        uint256 navEnd; // at the end of the epoch (after the next poke's checkpoint)
        uint256 loss; // NAV_before(ref) − NAV_after(ref) of the action
        int256 dPassive; // the passive leg's change over [after the action, end]
        uint256 sqrt0; // the reference's sqrt price after the action and at the end
        uint256 sqrt1;
        uint256 fees0; // the fees per L_REF on the mandate range over the window (token0, token1)
        uint256 fees1;
        int24 lo; // the curator's range and the mandate range
        int24 hi;
        int24 wlo;
        int24 whi;
    }

    function _out() internal view returns (string memory) {
        return vm.envOr("TS_E0_OUT", string(""));
    }

    function _ref() internal view returns (int24) {
        return _obs().refTick;
    }

    function _navRef() internal view returns (uint256 nav) {
        (nav,) = ExecLedger.value(hv.holdings(), TickMath.getSqrtPriceAtTick(_ref()));
    }

    uint32[] internal _ps;
    int24[] internal _pt;

    /// @dev The reference follows the pool's tick. The mock's TWAP walks its whole path, so the path is kept short: the
    ///      segments that cover the last hour (the TWAP window and the markout windows are shorter) plus the new one.
    function _follow() internal {
        uint32 now_ = uint32(vm.getBlockTimestamp());
        int24 t = _tick();
        if (_ps.length > 0 && _ps[_ps.length - 1] == now_) {
            _pt[_pt.length - 1] = t;
        } else {
            _ps.push(now_);
            _pt.push(t);
        }
        uint256 first;
        while (first + 1 < _ps.length && _ps[first + 1] + 3600 <= now_) ++first;
        uint256 n = _ps.length - first;
        uint32[] memory s = new uint32[](n);
        int24[] memory k = new int24[](n);
        for (uint256 i; i < n; ++i) (s[i], k[i]) = (_ps[first + i], _pt[first + i]);
        // the first segment reaches back far enough for any window
        if (s[0] + 3600 > now_) s[0] = now_ > 7200 ? now_ - 7200 : 1;
        refPool.setPath(s, k);
    }

    /// @dev One epoch of third-party trading: a seeded random walk, one swap per 4 blocks, the reference following.
    function _trade(uint256 seed) internal {
        for (uint256 i; i < tier.EPOCH / 4; ++i) {
            uint256 r = uint256(keccak256(abi.encode(seed, i)));
            bool z = r & 1 == 1;
            // keep the walk within ±300 ticks of the seed tick (a walk that drains one token leaves the curator's
            // re-placement short of DELTA_MIN without a pre-swap)
            int24 t = _tick();
            if (t > 300) z = true;
            if (t < -300) z = false;
            _swap(z, 1e19 + (r >> 8) % 4e19, 60);
            advance(4);
            _follow();
            ITouchstoneHook(hook).refreshRef();
        }
    }

    /// @dev The hook's live witness (slots 1 and 3 of its layout snapshot: `_witLo` at byte 24, `_witHi` at 27,
    ///      `_witEpoch` at slot 3 byte 0).
    function _witness() internal view returns (int24 lo, int24 hi, uint32 e) {
        uint256 w1 = uint256(vm.load(hook, bytes32(uint256(1))));
        uint256 w3 = uint256(vm.load(hook, bytes32(uint256(3))));
        lo = int24(uint24(w1 >> 192));
        hi = int24(uint24(w1 >> 216));
        e = uint32(w3);
    }

    function _feeInside(int24 lo, int24 hi) internal view returns (uint256 g0, uint256 g1) {
        (g0, g1) = manager.getFeeGrowthInside(poolId, lo, hi);
    }

    function _run(string memory name, int24 offset) internal {
        if (bytes(_out()).length == 0) return;
        string memory json = "[";
        IVaultEntries(address(hv)).poke(); // the witness on the mandate range of the current epoch
        for (uint256 k; k < EPOCHS; ++k) {
            // align to just after an epoch boundary, poke (the roll and the witness), then the curator's re-placement
            advance(tier.EPOCH - vm.getBlockNumber() % tier.EPOCH);
            _follow();
            ITouchstoneHook(hook).refreshRef();
            IVaultEntries(address(hv)).poke();
            (int24 wlo, int24 whi, uint32 we) = _witness();
            Ep memory ep;
            ep.e = we;
            (ep.lo, ep.hi, ep.wlo, ep.whi) = (wlo + offset, whi + offset, wlo, whi);
            int24 lo = wlo + offset;
            int24 hi = whi + offset;
            uint256 navBefore = _navRef();
            vm.prank(curator);
            hv.setPositions(lo, hi, PreSwap(false, false, 0), 0, type(uint256).max);
            ep.navAfter = _navRef();
            ep.loss = navBefore > ep.navAfter ? navBefore - ep.navAfter : 0;
            // the passive leg: the vault's post-action holdings with its L on the mandate range
            Holdings memory h = hv.holdings();
            Holdings memory p = Holdings(h.idle0, h.idle1, h.L, wlo, whi);
            uint160 s0 = TickMath.getSqrtPriceAtTick(_ref());
            (uint256 pv0,) = ExecLedger.value(p, s0);
            (uint256 f0a, uint256 f1a) = _feeInside(wlo, whi);
            _trade(k + (offset == 0 ? 0 : 1000));
            advance(tier.EPOCH - vm.getBlockNumber() % tier.EPOCH); // the next boundary
            _follow();
            ITouchstoneHook(hook).refreshRef();
            hv.settle(); // the checkpoint that rolls (closes the record), collect-first
            uint160 s1 = TickMath.getSqrtPriceAtTick(_ref());
            ep.navEnd = _navRef();
            (uint256 pv1,) = ExecLedger.value(p, s1);
            (uint256 f0b, uint256 f1b) = _feeInside(wlo, whi);
            uint256 fv0;
            uint256 fv1;
            unchecked {
                fv0 = FullMath.mulDiv(f0b - f0a, h.L, FixedPoint128.Q128);
                fv1 = FullMath.mulDiv(f1b - f1a, h.L, FixedPoint128.Q128);
                ep.fees0 = FullMath.mulDiv(f0b - f0a, 1e18, FixedPoint128.Q128);
                ep.fees1 = FullMath.mulDiv(f1b - f1a, 1e18, FixedPoint128.Q128);
            }
            // the fees valued at the end price (token0 at s1², token1 at 1)
            uint256 feeVal = FullMath.mulDiv(FullMath.mulDiv(fv0, s1, 1 << 96), s1, 1 << 96) + fv1;
            ep.dPassive = int256(pv1) + int256(feeVal) - int256(pv0);
            (ep.sqrt0, ep.sqrt1) = (s0, s1);
            EpochRecord memory r = hv.record(we);
            json = string.concat(json, k == 0 ? "" : ",", _epJson(ep, r, h.L));
            // the curator's markout settles before the next action
        }
        json = string.concat(json, "]");
        vm.writeFile(string.concat(_out(), "/E0-", name, ".json"), json);
    }

    function _epJson(Ep memory ep, EpochRecord memory r, uint128 L) internal pure returns (string memory) {
        string memory a = string.concat(
            '{"e":', vm.toString(ep.e), ',"navAfter":"', vm.toString(ep.navAfter), '","navEnd":"', vm.toString(ep.navEnd),
            '","loss":"', vm.toString(ep.loss), '","dPassive":"', vm.toString(ep.dPassive), '","sqrt0":"',
            vm.toString(ep.sqrt0), '","sqrt1":"', vm.toString(ep.sqrt1), '","lo":', vm.toString(int256(ep.lo)), ',"hi":',
            vm.toString(int256(ep.hi)), ',"wlo":', vm.toString(int256(ep.wlo)), ',"whi":', vm.toString(int256(ep.whi)), ',"pad":"'
        );
        string memory b = string.concat(
            '","fees0":"', vm.toString(ep.fees0), '","fees1":"', vm.toString(ep.fees1), '","L":"', vm.toString(uint256(L)),
            '","start":', vm.toString(uint256(r.start)), ',"end":', vm.toString(uint256(r.end)), ',"med9Start":',
            vm.toString(int256(r.med9Start)), ',"med9End":', vm.toString(int256(r.med9End))
        );
        string memory c = string.concat(
            ',"tauV":', vm.toString(uint256(r.tauV)), ',"tauB":', vm.toString(uint256(r.tauB)), ',"lTau":"',
            vm.toString(uint256(r.lTau)), '","charge":"', vm.toString(uint256(r.charge)), '","base":"',
            vm.toString(uint256(r.base)), '","minted":"', vm.toString(uint256(r.minted)), '","flags":',
            vm.toString(uint256(r.flags)), "}"
        );
        return string.concat(a, b, c);
    }

    function test_E0data__sameRange() public {
        _run("same", 0);
    }

    function test_E0data__offsetRange() public {
        _run("offset", OFFSET);
    }
}
