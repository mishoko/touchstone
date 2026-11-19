// SPDX-License-Identifier: MIT
pragma solidity 0.8.26;

import {Vm} from "forge-std/Vm.sol";
import {IERC20} from "@openzeppelin/contracts/token/ERC20/IERC20.sol";
import {StateLibrary} from "@uniswap/v4-core/src/libraries/StateLibrary.sol";
import {TickMath} from "@uniswap/v4-core/src/libraries/TickMath.sol";
import {FullMath} from "@uniswap/v4-core/src/libraries/FullMath.sol";
import {FixedPoint128} from "@uniswap/v4-core/src/libraries/FixedPoint128.sol";
import {SwapParams} from "@uniswap/v4-core/src/types/PoolOperation.sol";
import {PoolSwapTest} from "@uniswap/v4-core/src/test/PoolSwapTest.sol";
import {LiquidPoolBase} from "../utils/LiquidPoolBase.sol";
import {MockRefPool} from "../utils/MockRefPool.sol";
import {ITouchstoneHook} from "../../src/interfaces/ITouchstoneHook.sol";
import {IVaultEntries} from "../../src/interfaces/ITouchstoneVaultExtension.sol";
import {VaultCommon} from "../../src/TouchstoneVaultBase.sol";
import {Holdings} from "../../src/types/Holdings.sol";
import {PreSwap} from "../../src/types/PreSwap.sol";
import {Tier} from "../../src/types/Tier.sol";
import {ExecLedger} from "../../src/libraries/ExecLedger.sol";

/// @notice E0b's data harness (spec §3.8, §10 "E0b frozen form"; design `internal` s25 E0b, gate passed at r10).
///         Self-contained: E0a's harness (`E0aData.t.sol`) copied, with E0a's three run functions kept verbatim as fit
///         data (`test_E0bData__fit_same/offset/exit`, run names `same`, `offset`, `exit`, which key the walk), plus:
///         - `E0b-<tag>-feereads.jsonl`: per vault call, PoolManager.getFeeGrowthInside of the vault's range read
///           immediately before the call (`pre`, the closing segment's range) and after it (`post`, the next segment's
///           range): the ledger's vault fee leg floor(L·Δg/2¹²⁸) (panel D6; an evaluator input, no `src/` change);
///         - `E0b-<tag>-witness.jsonl`: fee growth inside the live witness range and the hook's `tauB`, one `step` row
///           per walked block right after its first touch (the refresh) and before any swap, and `pre`/`post` rows at
///           each vault call (non-vacuity only, never read by the ledger);
///         - `E0b-<tag>-ops.jsonl`: one row per executed script op (its blocks; reverted vault calls with their
///           discarded log count);
///         - `test_E0bData__script`: executes a generated op list (`eval/e0b_gen.py`, through `eval/e0b_run.py` only),
///           with each vault call wrapped so that a revert is recorded and the script continues.
///         Run only through `python3 eval/e0b_run.py (seed:<n> | fit:<name>) <outdir>`.
contract E0bDataTest is LiquidPoolBase {
    using StateLibrary for *;

    /// @dev The raised base rate (disclosed): ≈ 1.2e-5 of NAV₀ per in-range block at δ = 1, ≈ 10 bp per epoch.
    uint64 internal constant M_RAISED = 1.2e13;
    int24 internal constant OFF = 600;
    uint256 internal constant L_REF = 1e18;

    enum K {
        Poke,
        Settle,
        Place,
        Claim,
        Retire,
        Deposit,
        Withdraw,
        ProcessBreach,
        Rebalance
    }

    string internal _runName;
    string internal _logsPath;
    string internal _truthPath;
    string internal _feePath;
    string internal _witPath;
    string internal _opsPath;
    bool internal _heldOut; // held-out script: vault calls are wrapped (a revert is recorded, the script continues)
    uint256 internal _ci; // the call index (one per transaction)
    uint256 internal _D; // the fixed depositor's shares (alice: no flows of her own)

    // the open segment (truth, from storage)
    bool internal _segOpen;
    Holdings internal _segH;
    uint256 internal _segS;
    int24 internal _segP0;
    int24 internal _segWlo;
    int24 internal _segWhi;
    uint256 internal _segFg0;
    uint256 internal _segFg1;
    uint256 internal _segLb;
    uint256 internal _gate; // hook calls in the segment with spot inside the vault's range and the cache outside
    // the passive leg's sizing (truth)
    uint256 internal _lbSized;
    uint256 internal _lSized;

    uint32[] internal _ps;
    int24[] internal _pt;

    function _adjustTier(Tier memory t) internal view override {
        t.m_perBlock = M_RAISED;
    }

    function _dir() internal view returns (string memory) {
        return vm.envOr("TS_E0B_OUT", string(""));
    }

    // ------------------------------------------------------------------ the reference and third-party trading

    /// @dev The reference follows the pool's tick at most once per 48 s. The mock walks its whole path in `observe` under
    ///      the reference gas cap, so only the last 1,200 s are kept (≈ 25 segments; every window the hook reads starts
    ///      within ≈ 1,060 s of now: the TWAP at an anchor, the markout window).
    function _follow() internal {
        uint32 now_ = uint32(vm.getBlockTimestamp());
        if (_ps.length > 0 && _ps[_ps.length - 1] + 48 > now_) return;
        _ps.push(now_);
        _pt.push(_tick());
        uint256 first;
        while (first + 1 < _ps.length && _ps[first + 1] + 1200 <= now_) ++first;
        uint256 n = _ps.length - first;
        uint32[] memory s = new uint32[](n);
        int24[] memory k = new int24[](n);
        for (uint256 i; i < n; ++i) (s[i], k[i]) = (_ps[first + i], _pt[first + i]);
        if (s[0] + 1200 > now_) s[0] = now_ > 7200 ? now_ - 7200 : 1;
        refPool.setPath(s, k);
    }

    function _refresh() internal {
        ITouchstoneHook(hook).refreshRef();
        _hookDone();
    }

    /// @dev A third-party swap to `target` (an exact input far above need, stopped at the price limit).
    function _swapTo(int24 target) internal {
        int24 t = _tick();
        if (t == target) return;
        bool z = target < t;
        swapRouter.swap(
            key,
            SwapParams({zeroForOne: z, amountSpecified: -int256(1e30), sqrtPriceLimitX96: TickMath.getSqrtPriceAtTick(target)}),
            PoolSwapTest.TestSettings({takeClaims: false, settleUsingBurn: false}),
            ""
        );
        _hookDone();
    }

    /// @dev Walks to block `blk`: one step per 2 blocks (the reference follows, a refresh, then a swap moving the price
    ///      20–30 ticks in a seeded random direction, held within ±60 ticks of `target`). The fixed step size keeps the
    ///      fee per in-range block close to uniform; the third-party liquidity (1e21 on ±6,000 ticks) dominates the
    ///      vault's, so the per-unit fee rate does not depend on whether the vault is in range.
    function _walkTo(uint256 blk, int24 target) internal {
        _walkI(blk, target, 1);
    }

    /// @dev E0a's walk at intensity 1 (unchanged: the same RNG key, steps 20–30, band ±60); intensity 0 refreshes
    ///      without a swap; intensity 3 steps 60–90 ticks within ±180. A witness `step` row follows each refresh.
    function _walkI(uint256 blk, int24 target, uint256 intensity) internal {
        while (vm.getBlockNumber() < blk) {
            advance(blk - vm.getBlockNumber() >= 2 ? 2 : 1);
            _follow();
            _refresh();
            _witRow("step");
            if (intensity == 0) continue;
            uint256 r = uint256(keccak256(abi.encode(_runName, vm.getBlockNumber())));
            int24 step = int24(int256(20 + r % 11)) * int24(int256(intensity));
            int24 band = 60 * int24(int256(intensity));
            int24 t = _tick();
            bool up = (r >> 8) & 1 == 1;
            if (t > target + band) up = false;
            if (t < target - band) up = true;
            _swapTo(up ? t + step : t - step);
        }
    }

    function _ep(uint256 k) internal view returns (uint256) {
        return _e0 + k * tier.EPOCH;
    }

    uint256 internal _e0;

    // ------------------------------------------------------------------ logs

    function _hookDone() internal {
        _drain();
        // the τ_v reference-gate tag: spot inside the vault's range while the cache is outside it
        Holdings memory h = hv.holdings();
        if (h.L > 0) {
            int24 s = _tick();
            int24 c = _obs().refTick;
            if (h.lo <= s && s < h.hi && !(h.lo <= c && c < h.hi)) ++_gate;
        }
        ++_ci;
    }

    function _name(Vm.Log memory l) internal view returns (string memory) {
        if (l.topics.length == 0) return "";
        bytes32 t = l.topics[0];
        if (l.emitter == vault) {
            if (t == VaultCommon.Checkpoint.selector) return "Checkpoint";
            if (t == VaultCommon.EpochRecorded.selector) return "EpochRecorded";
            if (t == VaultCommon.BreachProcessed.selector) return "BreachProcessed";
            if (t == VaultCommon.Retired.selector) return "Retired";
            if (t == VaultCommon.Settled.selector) return "Settled";
            if (t == IERC20.Transfer.selector) return "Transfer";
        } else if (l.emitter == hook) {
            if (t == ITouchstoneHook.RefRefreshed.selector) return "RefRefreshed";
            if (t == ITouchstoneHook.RangeRegistered.selector) return "RangeRegistered";
            if (t == ITouchstoneHook.WitnessRolled.selector) return "WitnessRolled";
        }
        return "";
    }

    /// @dev Writes the call's logs raw and returns the vault's own mints to and burns from itself (truth).
    function _drain() internal returns (uint256 mints, uint256 burns) {
        Vm.Log[] memory logs = vm.getRecordedLogs();
        for (uint256 i; i < logs.length; ++i) {
            Vm.Log memory l = logs[i];
            string memory nm = _name(l);
            if (bytes(nm).length == 0) continue;
            string memory tp = "[";
            for (uint256 j; j < l.topics.length; ++j) {
                tp = string.concat(tp, j == 0 ? '"' : ',"', vm.toString(l.topics[j]), '"');
            }
            vm.writeLine(
                _logsPath,
                string.concat(
                    '{"c":', vm.toString(_ci), ',"b":', vm.toString(vm.getBlockNumber()), ',"i":', vm.toString(i),
                    ',"ev":"', nm, '","topics":', tp, '],"data":"', vm.toString(l.data), '"}'
                )
            );
            if (l.emitter == vault && l.topics[0] == IERC20.Transfer.selector) {
                address from = address(uint160(uint256(l.topics[1])));
                address to = address(uint160(uint256(l.topics[2])));
                uint256 amt = abi.decode(l.data, (uint256));
                if (from == address(0) && to == vault) mints += amt;
                if (from == vault && to == address(0)) burns += amt;
            }
        }
    }

    // ------------------------------------------------------------------ truth helpers (storage only)

    function _cache() internal view returns (int24) {
        return _obs().refTick;
    }

    function _val(Holdings memory h, int24 tick) internal view returns (uint256 nav) {
        (nav,) = ExecLedger.value(h, TickMath.getSqrtPriceAtTick(tick));
    }

    /// @dev The hook's live witness from its storage (slot 1: `_witLo` at byte 24, `_witHi` at 27; slot 3: `_witEpoch`,
    ///      `_witL`).
    function _witness() internal view returns (int24 lo, int24 hi, uint32 e, uint128 l) {
        uint256 w1 = uint256(vm.load(hook, bytes32(uint256(1))));
        uint256 w3 = uint256(vm.load(hook, bytes32(uint256(3))));
        lo = int24(uint24(w1 >> 192));
        hi = int24(uint24(w1 >> 216));
        e = uint32(w3);
        l = uint128(w3 >> 32);
    }

    /// @dev The vault position's uncollected fees (what the next collect-first books into idle).
    function _owed(Holdings memory h) internal view returns (uint256 o0, uint256 o1) {
        if (h.L == 0) return (0, 0);
        (uint128 liq, uint256 l0, uint256 l1) = manager.getPositionInfo(poolId, vault, h.lo, h.hi, bytes32(0));
        (uint256 g0, uint256 g1) = manager.getFeeGrowthInside(poolId, h.lo, h.hi);
        unchecked {
            o0 = FullMath.mulDiv(g0 - l0, liq, FixedPoint128.Q128);
            o1 = FullMath.mulDiv(g1 - l1, liq, FixedPoint128.Q128);
        }
    }

    /// @dev `L_b` at equal value with the vault's position at `tick`: `L · u(lo, hi, p) / u(wlo, whi, p)`.
    function _size(Holdings memory h, int24 wlo, int24 whi, int24 tick) internal view returns (uint256) {
        if (h.L == 0) return 0;
        uint256 vV = _val(Holdings(0, 0, h.L, h.lo, h.hi), tick);
        uint256 vW = _val(Holdings(0, 0, 1e24, wlo, whi), tick);
        return FullMath.mulDiv(vV, 1e24, vW);
    }

    // ------------------------------------------------------------------ vault calls

    function _place(int24 lo, int24 hi) internal {
        // keep spot within the fence of the live reference (third-party trading)
        (bool ok, int24 live) = ITouchstoneHook(hook).tryRef();
        require(ok, "ref");
        int24 s = _tick();
        if (s > live + 30 || s < live - 30) _swapTo(live + (s > live ? int24(15) : int24(-15)));
        _v(K.Place, lo, hi, 0);
    }

    function _wit() internal view returns (int24 lo, int24 hi) {
        (lo, hi,,) = _witness();
    }

    struct Pre {
        Holdings h;
        Holdings mid;
        uint256 S;
        int24 c;
        uint256 nav;
        uint256 fg0;
        uint256 fg1;
        uint32 we;
    }

    function _v(K k, int24 lo, int24 hi, uint256 amt) internal {
        Pre memory p;
        p.h = hv.holdings();
        (uint256 o0, uint256 o1) = _owed(p.h);
        p.mid = Holdings(uint128(p.h.idle0 + o0), uint128(p.h.idle1 + o1), p.h.L, p.h.lo, p.h.hi);
        p.S = hv.totalSupply();
        p.c = _cache();
        p.nav = _val(p.h, p.c);
        if (_segOpen) (p.fg0, p.fg1) = manager.getFeeGrowthInside(poolId, _segWlo, _segWhi);
        (,, p.we,) = _witness();
        (uint256 v0, uint256 v1) = manager.getFeeGrowthInside(poolId, p.h.lo, p.h.hi);
        string memory witPre = _witJson("pre");

        if (_heldOut) {
            try this.callVault(k, lo, hi, amt, p.nav) {}
            catch (bytes memory reason) {
                uint256 n = vm.getRecordedLogs().length; // discarded: a reverted call books nothing
                vm.writeLine(
                    _opsPath,
                    string.concat(
                        '{"op":"revert","k":', vm.toString(uint256(k)), ',"b":', vm.toString(vm.getBlockNumber()),
                        ',"discarded":', vm.toString(n), ',"reason":"', vm.toString(reason), '"}'
                    )
                );
                return;
            }
        } else {
            _callVault(k, lo, hi, amt, p.nav);
        }
        _feeRow(p.h, v0, v1);
        vm.writeLine(_witPath, witPre);
        _witRow("post");
        (uint256 mints, uint256 burns) = _drain();
        _truth(k, p, mints, burns);
        ++_ci;
    }

    /// @dev The held-out wrapper's external entry (so a revert can be caught); only this contract may call it.
    function callVault(K k, int24 lo, int24 hi, uint256 amt, uint256 nav) external {
        require(msg.sender == address(this), "self");
        _callVault(k, lo, hi, amt, nav);
    }

    function _callVault(K k, int24 lo, int24 hi, uint256 amt, uint256 nav) internal {
        Pre memory p;
        p.nav = nav;
        if (k == K.Poke) {
            IVaultEntries(vault).poke();
        } else if (k == K.Settle) {
            hv.settle();
        } else if (k == K.ProcessBreach) {
            IVaultEntries(vault).processBreach();
        } else if (k == K.Place) {
            // a placement whose idle cannot support δ ≥ DELTA_MIN on the new range adds a pre-swap of 1.5% of NAV (an
            // ordinary charged action, under the 2% per-action cap), in whichever direction fits
            vm.prank(curator);
            try hv.setPositions(lo, hi, PreSwap(false, false, 0), 0, type(uint256).max) {}
            catch {
                uint128 a = uint128(p.nav * 15 / 1000);
                vm.prank(curator);
                try hv.setPositions(lo, hi, PreSwap(true, true, a), 0, type(uint256).max) {}
                catch {
                    vm.prank(curator);
                    hv.setPositions(lo, hi, PreSwap(true, false, a), 0, type(uint256).max);
                }
            }
        } else if (k == K.Claim) {
            vm.prank(curator);
            IVaultEntries(vault).claim();
        } else if (k == K.Retire) {
            vm.prank(curator);
            IVaultEntries(vault).retire();
        } else if (k == K.Deposit) {
            _deposit(bob, amt);
        } else if (k == K.Withdraw) {
            _withdraw(bob, amt);
        } else {
            _rebalance(amt);
        }
    }

    function _truth(K k, Pre memory p, uint256 mints, uint256 burns) internal {
        Holdings memory h = hv.holdings();
        uint256 S = hv.totalSupply();
        int24 c = _cache();
        uint256 nav = _val(h, c);
        string memory a = string.concat(
            '{"c":', vm.toString(_ci), ',"b":', vm.toString(vm.getBlockNumber()), ',"k":', vm.toString(uint256(k)),
            ',"epoch":', vm.toString(uint256(hv.scalars().epoch)), ',"D":"', vm.toString(_D), '","Spre":"',
            vm.toString(p.S), '","S":"', vm.toString(S), '","navPre":"', vm.toString(p.nav), '","nav":"',
            vm.toString(nav), '","navMid":"', vm.toString(_val(p.mid, c))
        );
        string memory b = string.concat(
            '","mints":"', vm.toString(mints), '","burns":"', vm.toString(burns), '","tickPre":', vm.toString(int256(p.c)),
            ',"tick":', vm.toString(int256(c)), ',"L":"', vm.toString(uint256(h.L)), '","lo":', vm.toString(int256(h.lo)),
            ',"hi":', vm.toString(int256(h.hi))
        );
        // the segment this call closes
        string memory seg = ',"seg":null';
        if (_segOpen) {
            Holdings memory pb = Holdings(_segH.idle0, _segH.idle1, uint128(_segLb), _segWlo, _segWhi);
            uint256 f0;
            uint256 f1;
            unchecked {
                f0 = FullMath.mulDiv(p.fg0 - _segFg0, _segLb, FixedPoint128.Q128);
                f1 = FullMath.mulDiv(p.fg1 - _segFg1, _segLb, FixedPoint128.Q128);
            }
            uint160 s1 = TickMath.getSqrtPriceAtTick(c);
            uint256 feeVal = FullMath.mulDiv(FullMath.mulDiv(f0, s1, 1 << 96), s1, 1 << 96) + f1;
            int256 dP = int256(_val(pb, c)) - int256(_val(pb, _segP0)) + int256(feeVal);
            seg = string.concat(
                ',"seg":{"S":"', vm.toString(_segS), '","dPassive":"', vm.toString(dP), '","Lb":"', vm.toString(_segLb),
                '","L":"', vm.toString(uint256(_segH.L)), '","p0":', vm.toString(int256(_segP0)), ',"p1":',
                vm.toString(int256(c)), ',"wlo":', vm.toString(int256(_segWlo)), ',"whi":', vm.toString(int256(_segWhi)),
                ',"gate":', vm.toString(_gate), "}"
            );
        }
        // sizing of the passive leg (truth: storage only)
        (int24 wlo, int24 whi, uint32 we, uint128 wl) = _witness();
        if (k == K.Place) {
            (_lSized, _lbSized) = (h.L, _size(h, wlo, whi, c));
        } else if (we != p.we && wl > 0) {
            (_lSized, _lbSized) = (h.L, _size(h, wlo, whi, ITouchstoneHook(hook).epochStartRef()));
        }
        uint256 lb = _lSized == 0 ? 0 : FullMath.mulDiv(_lbSized, h.L, _lSized);
        string memory d = string.concat(
            ',"wlo":', vm.toString(int256(wlo)), ',"whi":', vm.toString(int256(whi)), ',"we":', vm.toString(uint256(we)),
            ',"wL":"', vm.toString(uint256(wl)), '","Lb":"', vm.toString(lb), '","baseOwed":"',
            vm.toString(uint256(hv.scalars().baseOwed)), '","fresh":', _obs().refFresh ? "true" : "false",
            ',"gap":', _obs().gapSinceVerify ? "true" : "false", "}"
        );
        vm.writeLine(_truthPath, string.concat(a, b, seg, d));
        // open the next segment
        _segOpen = true;
        _segH = h;
        _segS = S;
        _segP0 = c;
        (_segWlo, _segWhi) = (wlo, whi);
        (_segFg0, _segFg1) = manager.getFeeGrowthInside(poolId, wlo, whi);
        _segLb = lb;
        _gate = 0;
    }

    // ------------------------------------------------------------------ runs

    function _begin(string memory run) internal returns (bool) {
        return _begin(run, string.concat("fit-", run));
    }

    function _begin(string memory run, string memory tag) internal returns (bool) {
        if (bytes(_dir()).length == 0) return false;
        _runName = run;
        _logsPath = string.concat(_dir(), "/E0b-", tag, "-logs.jsonl");
        _truthPath = string.concat(_dir(), "/E0b-", tag, "-truth.jsonl");
        _feePath = string.concat(_dir(), "/E0b-", tag, "-feereads.jsonl");
        _witPath = string.concat(_dir(), "/E0b-", tag, "-witness.jsonl");
        _opsPath = string.concat(_dir(), "/E0b-", tag, "-ops.jsonl");
        if (vm.exists(_logsPath)) vm.removeFile(_logsPath);
        if (vm.exists(_truthPath)) vm.removeFile(_truthPath);
        if (vm.exists(_feePath)) vm.removeFile(_feePath);
        if (vm.exists(_witPath)) vm.removeFile(_witPath);
        if (vm.exists(_opsPath)) vm.removeFile(_opsPath);
        vm.writeFile(
            string.concat(_dir(), "/E0b-tier.json"),
            string.concat(
                '{"W_TICKS":', vm.toString(int256(tier.W_TICKS)), ',"TICK_SPACING":', vm.toString(int256(TICK_SPACING)),
                ',"EPOCH":', vm.toString(uint256(tier.EPOCH)), ',"FORFEIT_DRIP_BLOCKS":',
                vm.toString(uint256(tier.FORFEIT_DRIP_BLOCKS)), ',"L_REF":"', vm.toString(L_REF), '","m_perBlock":"',
                vm.toString(uint256(tier.m_perBlock)), '"}'
            )
        );
        _D = hv.balanceOf(alice);
        vm.recordLogs();
        advance(2);
        _follow();
        _refresh();
        _e0 = (vm.getBlockNumber() / tier.EPOCH + 1) * tier.EPOCH;
        return true;
    }

    /// @dev The epoch's poke at `+22` (after the epoch's first cache write), then the curator's placement at `+25`.
    function _pokePlace(uint256 k, int24 target, int24 off) internal {
        _walkTo(_ep(k) + 22, target);
        _v(K.Poke, 0, 0, 0);
        _walkTo(_ep(k) + 25, target);
        (int24 wlo, int24 whi) = _wit();
        _place(wlo + off, whi + off);
    }

    function _centre() internal view returns (int24) {
        (int24 wlo, int24 whi) = _wit();
        return (wlo + whi) / 2;
    }

    function test_E0bData__fit_same() public {
        if (!_begin("same")) return;
        _pokePlace(0, 0, 0);
        for (uint256 k = 1; k <= 7; ++k) {
            int24 t = _centre() + (k % 2 == 0 ? int24(-80) : int24(80));
            _pokePlace(k, t, 0);
            if (k == 3) {
                // a failed settlement read: the whole reserve burns (the cache is still fresh: the base is minted)
                _walkTo(_ep(k) + 30, t);
                refPool.setMode(MockRefPool.Mode.Revert);
                _walkTo(_ep(k) + 40, t);
                _v(K.Settle, 0, 0, 0);
                refPool.setMode(MockRefPool.Mode.Ok);
            }
            _walkTo(_ep(k) + 70, t);
            _v(K.Settle, 0, 0, 0); // a dust checkpoint
        }
        _walkTo(_ep(8) + 22, _centre());
        _v(K.Poke, 0, 0, 0); // the closing poke, in an epoch after the last roll
    }

    function test_E0bData__fit_offset() public {
        if (!_begin("offset")) return;
        _pokePlace(0, 100, OFF);
        uint256 bobShares;
        for (uint256 k = 1; k <= 8; ++k) {
            int24 t = 100 + (k % 2 == 0 ? int24(-70) : int24(70)); // an oscillation around a fixed tick (no ratchet)
            if (k == 4) {
                // a non-poke-first epoch: a settle rolls it, the price drifts, then the poke and the placement
                _walkTo(_ep(k) + 2, t);
                _v(K.Settle, 0, 0, 0);
                _walkTo(_ep(k) + 40, t + 60);
                _v(K.Poke, 0, 0, 0);
                _walkTo(_ep(k) + 43, t + 60);
                (int24 wlo, int24 whi) = _wit();
                _place(wlo + OFF, whi + OFF);
            } else {
                _pokePlace(k, t, OFF);
            }
            if (k == 2) {
                // three more segments: dust checkpoints and another holder's deposit
                _walkTo(_ep(k) + 40, t);
                _v(K.Settle, 0, 0, 0);
                _walkTo(_ep(k) + 50, t);
                bobShares = hv.totalSupply() / 100;
                _v(K.Deposit, 0, 0, bobShares);
                _walkTo(_ep(k) + 70, t);
                _v(K.Settle, 0, 0, 0);
                _walkTo(_ep(k) + 90, t);
                _v(K.Settle, 0, 0, 0);
            }
            if (k == 3) {
                // an unobserved in-range span (no touch for 60 blocks, more than the cache's freshness)
                _walkTo(_ep(k) + 50, t);
                advance(60);
                _follow();
            }
            if (k == 5) {
                _walkTo(_ep(k) + 50, t);
                _v(K.Withdraw, 0, 0, hv.balanceOf(bob));
            }
            if (k == 7) {
                _walkTo(_ep(k) + 50, t);
                _v(K.Claim, 0, 0, 0);
            }
        }
        _walkTo(_ep(9) + 22, 100);
        _v(K.Poke, 0, 0, 0);
    }

    /// @dev A stale-cache window before an epoch boundary: reads fail from `+82` of the previous epoch, so the poke at
    ///      `+2` has no fresh cache and no live read (an unpriced checkpoint) and no cache write in its epoch (a
    ///      fallback roll); reads resume at `+4`.
    function _stalePoke(uint256 k, int24 target) internal {
        _walkTo(_ep(k) - 38, target);
        refPool.setMode(MockRefPool.Mode.Revert);
        _walkTo(_ep(k) + 2, target);
        _v(K.Poke, 0, 0, 0);
        _walkTo(_ep(k) + 4, target);
        refPool.setMode(MockRefPool.Mode.Ok);
    }

    function test_E0bData__fit_exit() public {
        if (!_begin("exit")) return;
        _pokePlace(0, 100, OFF);
        for (uint256 k = 1; k <= 2; ++k) {
            _pokePlace(k, _centre() + 100 + (k % 2 == 0 ? int24(-60) : int24(60)), OFF);
        }
        // epoch 3: placed, a drift of ≥ 2 spacings between its first and last cache writes, a second placement at +45
        int24 c3 = _centre();
        _pokePlace(3, c3 + 60, OFF);
        _walkTo(_ep(3) + 40, c3 + 160);
        _v(K.Settle, 0, 0, 0);
        (int24 vlo, int24 vhi) = (hv.holdings().lo, hv.holdings().hi);
        _walkTo(_ep(3) + 45, c3 + 160);
        _place(vlo, vhi);
        // epoch 4's poke: the settlement read fails (the reserve burns), the checkpoint is unpriced (the base is
        // deferred), the witness rolls on the fallback centre (epoch 3's first write)
        _stalePoke(4, c3 + 160);
        // the price leaves the vault's range downwards and stays inside the witness range. Disclosed (fix review S21F-02):
        // this exit is one in-block swap of ≈ 400 ticks, right after a stale poke, so the vault's fees from the crossing
        // earn no τ_v credit; it is the run's one deliberate departure from the near-uniform walk of `_walkTo`.
        int24 out = vlo - 250;
        _swapTo(vlo - 150);
        uint32 seq0 = _obs().breachSeq;
        for (uint256 i; _obs().breachSeq == seq0; ++i) {
            require(i < 8, "no breach");
            uint256 next = (vm.getBlockNumber() / tier.EPOCH + 1) * tier.EPOCH;
            // walk towards the next boundary in small slices, stopping once a breach latches
            while (vm.getBlockNumber() < next - 38 && _obs().breachSeq == seq0) {
                _walkTo(vm.getBlockNumber() + 2, out);
            }
            if (_obs().breachSeq != seq0) break;
            // every vault checkpoint before the breach stays unpriced, so the deferred base is never minted
            refPool.setMode(MockRefPool.Mode.Revert);
            _walkTo(next + 2, out);
            _v(K.Poke, 0, 0, 0);
            _walkTo(next + 4, out);
            refPool.setMode(MockRefPool.Mode.Ok);
        }
        // the breach: forfeits the cohorts and the deferred base (processed here, or by a stale poke that met it);
        // the forfeit drips over the next epoch
        if (_obs().pendingBreaches != 0) _v(K.ProcessBreach, 0, 0, 0);
        uint256 b = vm.getBlockNumber();
        // the curator re-places around the live reference (BREACHED), accrues a cohort, places again (its reserve
        // partly from the cohort), retires inside the drip window, and the forfeited reserve settles after it
        _walkTo(b + 40, out);
        (, int24 live) = ITouchstoneHook(hook).tryRef();
        int24 rb = live / TICK_SPACING * TICK_SPACING;
        if (live < 0 && live % TICK_SPACING != 0) rb -= TICK_SPACING;
        (int24 nlo, int24 nhi) = (rb - 400, rb - 400 + tier.W_TICKS);
        int24 cn = rb + 100;
        _place(nlo, nhi);
        _walkTo(b + 56, cn);
        _v(K.Settle, 0, 0, 0);
        _walkTo(b + 58, cn);
        _place(nlo, nhi);
        _walkTo(b + 62, cn);
        _v(K.Retire, 0, 0, 0);
        _walkTo(b + 75, cn);
        _v(K.Settle, 0, 0, 0);
        // the drip runs out; the closing pokes
        uint256 e = vm.getBlockNumber() / tier.EPOCH + 1;
        _walkTo(e * tier.EPOCH + 22, cn);
        _v(K.Poke, 0, 0, 0);
        _walkTo((e + 1) * tier.EPOCH + 22, cn);
        _v(K.Poke, 0, 0, 0);
    }

    // ------------------------------------------------------------------ E0b reads (the fee leg; the witness series)

    function _feeRow(Holdings memory pre, uint256 v0, uint256 v1) internal {
        Holdings memory h = hv.holdings();
        (uint256 w0, uint256 w1) = manager.getFeeGrowthInside(poolId, h.lo, h.hi);
        vm.writeLine(
            _feePath,
            string.concat(
                '{"c":', vm.toString(_ci), ',"b":', vm.toString(vm.getBlockNumber()), ',"pre":{"lo":',
                vm.toString(int256(pre.lo)), ',"hi":', vm.toString(int256(pre.hi)), ',"L":"', vm.toString(uint256(pre.L)),
                '","g0":"', vm.toString(v0), '","g1":"', vm.toString(v1), '"},"post":{"lo":', vm.toString(int256(h.lo)),
                ',"hi":', vm.toString(int256(h.hi)), ',"L":"', vm.toString(uint256(h.L)), '","g0":"', vm.toString(w0),
                '","g1":"', vm.toString(w1), '"}}'
            )
        );
    }

    function _witJson(string memory kind) internal view returns (string memory) {
        (int24 wlo, int24 whi,,) = _witness();
        (uint256 g0, uint256 g1) = manager.getFeeGrowthInside(poolId, wlo, whi);
        return string.concat(
            '{"b":', vm.toString(vm.getBlockNumber()), ',"kind":"', kind, '","c":', vm.toString(_ci), ',"wlo":',
            vm.toString(int256(wlo)), ',"whi":', vm.toString(int256(whi)), ',"g0":"', vm.toString(g0), '","g1":"',
            vm.toString(g1), '","tauB":', vm.toString(uint256(_obs().tauB)), ',"tick":', vm.toString(int256(_tick())),
            "}"
        );
    }

    function _witRow(string memory kind) internal {
        if (bytes(_witPath).length > 0) vm.writeLine(_witPath, _witJson(kind));
    }

    /// @dev A curator `rebalanceSwap` of `bps`/10,000 of NAV (at the cached reference), from whichever idle token holds
    ///      more value (the direction the idle allows).
    function _rebalance(uint256 bps) internal {
        Holdings memory h = hv.holdings();
        uint160 sp = TickMath.getSqrtPriceAtTick(_cache());
        uint256 v0 = FullMath.mulDiv(FullMath.mulDiv(h.idle0, sp, 1 << 96), sp, 1 << 96);
        uint256 value = _val(h, _cache()) * bps / 10_000;
        bool zfo = v0 >= h.idle1;
        uint256 amt = zfo ? FullMath.mulDiv(FullMath.mulDiv(value, 1 << 96, sp), 1 << 96, sp) : value;
        uint256 cap = (zfo ? h.idle0 : h.idle1) * 9 / 10; // legality: within the idle (SwapCap)
        if (amt > cap) amt = cap;
        vm.prank(curator);
        hv.rebalanceSwap(zfo, uint128(amt), type(uint256).max);
    }

    // ------------------------------------------------------------------ the held-out script interpreter

    uint256 internal constant OP_W = 6; // [code, epoch, offset, a, b, c]

    function _op(uint256 code, uint256 b0, uint256 b1) internal {
        vm.writeLine(
            _opsPath,
            string.concat(
                '{"op":', vm.toString(code), ',"b0":', vm.toString(b0), ',"b1":', vm.toString(b1), "}"
            )
        );
    }

    /// @dev The walk's target for epoch k: the fixed tick 100 ± 80 by parity (E0a's `offset` pattern: no ratchet).
    function _tgt(uint256 k) internal pure returns (int24) {
        return 100 + (k % 2 == 0 ? int24(-80) : int24(80));
    }

    /// @dev Advances to `blk` without touching the pool (only when already there or behind; a walk op precedes it).
    function _at(uint256 blk) internal {
        if (vm.getBlockNumber() < blk) advance(blk - vm.getBlockNumber());
    }

    function test_E0bData__script() public {
        string memory path = vm.envOr("TS_E0B_SCRIPT", string(""));
        if (bytes(path).length == 0) return;
        string memory run = vm.envString("TS_E0B_RUN");
        require(_begin(run, run), "dir");
        _heldOut = true;
        int256[] memory ops = vm.parseJsonIntArray(vm.readFile(path), ".ops");
        require(ops.length % OP_W == 0 && ops.length > 0, "ops");
        for (uint256 i; i < ops.length; i += OP_W) {
            uint256 code = uint256(ops[i]);
            uint256 k = uint256(ops[i + 1]);
            uint256 blk = _ep(k) + uint256(ops[i + 2]);
            int256 a = ops[i + 3];
            int256 b = ops[i + 4];
            int256 c = ops[i + 5];
            uint256 b0 = vm.getBlockNumber();
            if (code == 0) {
                _walkI(blk, _tgt(k), uint256(a)); // WALK to blk at intensity a
                continue;
            }
            _at(blk);
            b0 = vm.getBlockNumber();
            if (code == 1) {
                _v(K.Poke, 0, 0, 0);
            } else if (code == 2) {
                _v(K.Settle, 0, 0, 0);
            } else if (code == 3) {
                (int24 wlo, int24 whi) = _wit();
                _place(wlo + int24(a), whi + int24(a)); // PLACE at offset a from the live witness
            } else if (code == 4) {
                refPool.setMode(MockRefPool.Mode.Revert); // STALE on
            } else if (code == 5) {
                refPool.setMode(MockRefPool.Mode.Ok); // STALE off
            } else if (code == 6) {
                // CROSS: a = edge (0 lower, 1 upper), b = depth past it, c = hold blocks with no touch; then the
                // return in one block (refresh first: the block's first touch and its witness row) to the target
                (int24 wlo, int24 whi) = _wit();
                _swapTo(a == 0 ? wlo - int24(b) : whi + int24(b));
                advance(uint256(c));
                _follow();
                _refresh();
                _witRow("step");
                _swapTo(_tgt(k));
            } else if (code == 7) {
                // REBAL a bps of NAV; legality as in `_place`: third-party trading first keeps spot within the fence
                (bool okr, int24 live) = ITouchstoneHook(hook).tryRef();
                if (okr) {
                    int24 sp = _tick();
                    if (sp > live + 30 || sp < live - 30) _swapTo(live + (sp > live ? int24(15) : int24(-15)));
                }
                _v(K.Rebalance, 0, 0, uint256(a));
            } else if (code == 8) {
                advance(uint256(a)); // UNOBSERVED span of a blocks
                _follow();
            } else if (code == 9) {
                // FLOW: a = 0 deposit / 1 withdraw, b = deposit bps of S, c = withdraw percent of bob's shares (a
                // withdraw with none held becomes a deposit of b)
                uint256 held = hv.balanceOf(bob);
                if (a == 1 && held > 0) {
                    _v(K.Withdraw, 0, 0, held * uint256(c) / 100);
                } else {
                    _v(K.Deposit, 0, 0, hv.totalSupply() * uint256(b) / 10_000);
                }
            } else if (code == 10) {
                _v(K.Claim, 0, 0, 0);
            } else {
                revert("op");
            }
            _op(code, b0, vm.getBlockNumber());
        }
        _op(99, vm.getBlockNumber(), vm.getBlockNumber()); // the script completed
    }
}
