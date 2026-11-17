// SPDX-License-Identifier: MIT
pragma solidity 0.8.26;

import {Vm} from "forge-std/Vm.sol";
import {IPoolManager} from "@uniswap/v4-core/src/interfaces/IPoolManager.sol";
import {IUnlockCallback} from "@uniswap/v4-core/src/interfaces/callback/IUnlockCallback.sol";
import {IHooks} from "@uniswap/v4-core/src/interfaces/IHooks.sol";
import {PoolKey} from "@uniswap/v4-core/src/types/PoolKey.sol";
import {PoolId} from "@uniswap/v4-core/src/types/PoolId.sol";
import {Currency} from "@uniswap/v4-core/src/types/Currency.sol";
import {BalanceDelta} from "@uniswap/v4-core/src/types/BalanceDelta.sol";
import {SwapParams, ModifyLiquidityParams} from "@uniswap/v4-core/src/types/PoolOperation.sol";
import {StateLibrary} from "@uniswap/v4-core/src/libraries/StateLibrary.sol";
import {TickMath} from "@uniswap/v4-core/src/libraries/TickMath.sol";
import {FullMath} from "@uniswap/v4-core/src/libraries/FullMath.sol";
import {PoolSwapTest} from "@uniswap/v4-core/src/test/PoolSwapTest.sol";
import {PoolModifyLiquidityTest} from "@uniswap/v4-core/src/test/PoolModifyLiquidityTest.sol";
import {IERC20} from "@openzeppelin/contracts/token/ERC20/IERC20.sol";
import {DeployBaseUnplaced} from "../utils/DeployBaseUnplaced.sol";
import {VaultView} from "../utils/VaultView.sol";
import {MockToken} from "../utils/Stubs.sol";
import {HookMining} from "../../script/Mine.s.sol";
import {CuratorBundler, DeployArgs} from "../../src/periphery/CuratorBundler.sol";
import {TouchstoneVault} from "../../src/TouchstoneVault.sol";
import {ITouchstoneHook} from "../../src/interfaces/ITouchstoneHook.sol";
import {HookArgs, SeedParams} from "../../src/interfaces/ITouchstoneFactory.sol";
import {ExecLedger} from "../../src/libraries/ExecLedger.sol";
import {LiquidityValue} from "../../src/libraries/LiquidityValue.sol";
import {Holdings} from "../../src/types/Holdings.sol";
import {PreSwap} from "../../src/types/PreSwap.sol";

using VaultView for TouchstoneVault;

/// @notice The attacker's trading account (the curator's Sybil, or an outside griefer): spot pushes and liquidity
///         positions on the vault's pool, settled from its own token balance.
contract Sybil is IUnlockCallback {
    using StateLibrary for IPoolManager;

    IPoolManager internal immutable M;
    PoolKey internal key;

    constructor(IPoolManager m, PoolKey memory k) {
        M = m;
        key = k;
    }

    /// @notice Moves the pool's spot to `target` (an exact-output swap of everything, stopped at the target).
    function pushTo(uint160 target) external {
        M.unlock(abi.encode(uint8(0), target, int24(0), int24(0), int256(0)));
    }

    /// @notice An exact-input swap of `amountIn`, stopped at `limit`.
    function swapIn(bool zeroForOne, uint256 amountIn, uint160 limit) external {
        M.unlock(abi.encode(uint8(2), limit, int24(zeroForOne ? int24(1) : int24(0)), int24(0), int256(amountIn)));
    }

    /// @notice Adds (or removes, `dL < 0`) liquidity on `[lo, hi)`; a removal also pays the accrued fees.
    function lp(int24 lo, int24 hi, int256 dL) external {
        M.unlock(abi.encode(uint8(1), uint160(0), lo, hi, dL));
    }

    function unlockCallback(bytes calldata data) external returns (bytes memory) {
        require(msg.sender == address(M), "pm");
        (uint8 kind, uint160 target, int24 lo, int24 hi, int256 dL) =
            abi.decode(data, (uint8, uint160, int24, int24, int256));
        BalanceDelta d;
        if (kind == 0) {
            (uint160 sp,,,) = M.getSlot0(PoolId.wrap(keccak256(abi.encode(key))));
            if (sp == target) return "";
            d = M.swap(key, SwapParams(target < sp, type(int128).max, target), "");
        } else if (kind == 2) {
            d = M.swap(key, SwapParams(lo == 1, -dL, target), "");
        } else {
            (d,) = M.modifyLiquidity(key, ModifyLiquidityParams(lo, hi, dL, bytes32(0)), "");
        }
        _settle(key.currency0, d.amount0());
        _settle(key.currency1, d.amount1());
        return "";
    }

    function _settle(Currency c, int128 a) internal {
        if (a < 0) {
            M.sync(c);
            IERC20(Currency.unwrap(c)).transfer(address(M), uint128(-a));
            M.settle();
        } else if (a > 0) {
            M.take(c, address(this), uint128(a));
        }
    }
}

/// @notice The manipulation cells' world (spec §5.1, §9.1 M-rows): the vault and its hook deployed through the factory
///         by the curator's `CuratorBundler` (its first placement in the deployment transaction), at the TEST tier, with
///         depositors up to the bond cap (so the curator holds the smallest share `BOND_RATIO` of the vault: the
///         defence's worst case), a scripted reference (`MockRefPool`) at the market price, a third party's swap and
///         liquidity routers, and the attacker's `Sybil`.
///
///         The coalition's net is measured after the markout settlement (the reserve burned or released): the
///         Sybil's tokens plus the curator's shares (bank, bond, pending reserve and any shares the bundler or its owner
///         hold), valued at the market price, after minus before. Each action's path is the production one: the
///         curator's calls go through the bundler to the vault.
abstract contract CellBase is DeployBaseUnplaced {
    using StateLibrary for *;

    uint256 internal constant WAD = 1e18;

    address internal owner = makeAddr("curatorOwner");
    address internal alice = makeAddr("alice");
    CuratorBundler internal bundler;
    TouchstoneVault internal v;
    PoolKey internal key;
    PoolId internal poolId;
    PoolSwapTest internal swapRouter;
    PoolModifyLiquidityTest internal lpRouter;
    Sybil internal syb;
    int24 internal W;
    /// @dev The market price (the reference's tick): what every holding is valued at.
    int24 internal market;

    /// @notice One action's outcome.
    struct Out {
        bool reverted;
        bytes4 err;
        /// the coalition's net: the Sybil's gain minus the value of the curator's shares burned (the charge and the
        /// reserve's burned part), at the market price after the settlement; the curator's pro-rata share of a NAV
        /// change every holder bears is left out (the owner-items panel's "curator net")
        int256 net;
        /// the coalition's wealth change (the Sybil's tokens plus the curator's shares at NAV per share); reported
        int256 wealth;
        uint256 charge0;
        uint256 extra;
        int256 sybil;
    }

    // ------------------------------------------------------------------ configuration

    /// @dev true: the USDC/WETH-like setting (tick 196,000, token0 at 6 decimals); false: tick 0, 18/18.
    function _highTick() internal pure virtual returns (bool) {
        return false;
    }

    function _seedTick() internal view virtual override returns (int24) {
        return _highTick() ? int24(196_000) : int24(0);
    }

    function _token0Decimals() internal view virtual override returns (uint8) {
        return _highTick() ? 6 : 0;
    }

    function _seedParams() internal view virtual override returns (SeedParams memory) {
        if (_highTick()) return SeedParams({a0: 3000e6, a1: 1e18, curator: address(0), wit0: 1e6, wit1: 1e12});
        return SeedParams({a0: 1e18, a1: 1e18, curator: address(0), wit0: 1e9, wit1: 1e9});
    }

    /// @dev The production vault; a positive control that needs a test seam overrides this with `VaultHarness`.
    function _vaultCode() internal pure virtual override returns (bytes memory) {
        return type(TouchstoneVault).creationCode;
    }

    /// @dev The deployment's first range (centred on the seed tick by default).
    function _firstLo() internal view virtual returns (int24) {
        return _floor(_seedTick()) - int24(int256(uint256(2000))) / 2;
    }

    // ------------------------------------------------------------------ setup

    function setUp() public virtual override {
        _setUpEnv();
        W = tier.W_TICKS;
        market = _seedTick();
        bundler = new CuratorBundler(owner, factory);
        address predicted = HookMining.vaultAddress(address(factory), address(bundler), SALT_V, _vaultCode());
        HookArgs memory ha = _hookArgsFor(predicted);
        (bytes32 sH,) = HookMining.mine(address(factory), address(bundler), _hookCode(), ha, 0);
        SeedParams memory sp = _seedParams();
        sp.curator = address(bundler);
        DeployArgs memory a = DeployArgs(_vaultCode(), _hookCode(), ha, SALT_V, sH, sp);
        token0.mint(address(bundler), 1e40);
        token1.mint(address(bundler), 1e40);
        int24 lo = _firstLo();
        vm.prank(owner);
        (vault, hook) = bundler.deployAndPlace(a, lo, lo + W, _none(), type(uint256).max);
        v = TouchstoneVault(vault);
        key = PoolKey(Currency.wrap(address(token0)), Currency.wrap(address(token1)), FEE, TICK_SPACING, IHooks(hook));
        poolId = PoolId.wrap(keccak256(abi.encode(key)));
        swapRouter = new PoolSwapTest(manager);
        lpRouter = new PoolModifyLiquidityTest(manager);
        token0.mint(address(this), 1e40);
        token1.mint(address(this), 1e40);
        token0.approve(address(swapRouter), type(uint256).max);
        token1.approve(address(swapRouter), type(uint256).max);
        token0.approve(address(lpRouter), type(uint256).max);
        token1.approve(address(lpRouter), type(uint256).max);
        syb = new Sybil(manager, key);
        token0.mint(address(syb), 1e40);
        token1.mint(address(syb), 1e40);
        _settleDue();
        _fillToCap();
    }

    /// @notice Alice deposits up to the bond cap (`S·BOND_RATIO ≤ bondShares·WAD`), less a margin of 1e-6.
    function _fillToCap() internal {
        token0.mint(alice, 1e40);
        token1.mint(alice, 1e40);
        vm.startPrank(alice);
        token0.approve(vault, type(uint256).max);
        token1.approve(vault, type(uint256).max);
        uint256 cap = v.bondShares() * WAD / tier.BOND_RATIO;
        uint256 s = cap - cap / 1e6 - v.totalSupply();
        v.deposit(s, type(uint256).max, type(uint256).max, alice);
        vm.stopPrank();
    }

    // ------------------------------------------------------------------ helpers

    function _none() internal pure returns (PreSwap memory) {
        return PreSwap(false, false, 0);
    }

    function _sqrt(int24 t) internal pure returns (uint160) {
        return TickMath.getSqrtPriceAtTick(t);
    }

    function _floor(int24 t) internal pure returns (int24) {
        int24 f = (t / TICK_SPACING) * TICK_SPACING;
        return (t < 0 && f != t) ? f - TICK_SPACING : f;
    }

    function _spot() internal view returns (uint160 sp, int24 t) {
        (sp, t,,) = manager.getSlot0(poolId);
    }

    function _h() internal view returns (Holdings memory) {
        return v.holdings();
    }

    /// @notice Rolls past the cooldown (which outlasts the markout window plus two blocks) and settles.
    function _settleDue() internal {
        advance(tier.COOLDOWN);
        v.settle();
    }

    /// @notice The reference (and the market) moves to `t` from now; the pool's spot is moved there by a third party;
    ///         then 51 blocks pass with keep-alive refreshes, so the reference's 10-minute TWAP reads `t` exactly.
    function _moveMarket(int24 t) internal {
        refPool.setTick(t);
        _thirdPartyPush(_sqrt(t));
        for (uint256 i; i < 51; i += 17) {
            advance(17);
            ITouchstoneHook(hook).refreshRef();
        }
        market = t;
        assertEq(ITouchstoneHook(hook).liveRef(), t, "the reference reads the market");
    }

    /// @notice A third party (not in the coalition) moves the pool's spot to `target`.
    function _thirdPartyPush(uint160 target) internal {
        (uint160 sp,) = _spot();
        if (sp == target) return;
        swapRouter.swap(
            key,
            SwapParams(target < sp, int256(uint256(type(uint128).max)), target),
            PoolSwapTest.TestSettings({takeClaims: false, settleUsingBurn: false}),
            ""
        );
    }

    /// @notice Third-party liquidity `k` (WAD) × the vault's L on `[floor(market) − 3000, floor(market) + 3000)`.
    function _thirdPartyDepth(uint256 k) internal {
        if (k == 0) return;
        uint256 L = _h().L;
        uint256 dl = FullMath.mulDiv(k, L == 0 ? 1e17 : L, WAD);
        int24 c = _floor(market);
        lpRouter.modifyLiquidity(key, ModifyLiquidityParams(c - 3000, c + 3000, int256(dl), 0), "");
    }

    /// @notice The curator's re-placement at the market (an honest action through the bundler), settled.
    function _honestPlace(int24 lo) internal {
        vm.prank(owner);
        bundler.setPositions(lo, lo + W, _none(), 0, type(uint256).max);
        _settleDue();
    }

    /// @notice A state with the position on `[lo, lo + W)` and the market at `r`: the curator places at a tick inside
    ///         the range nearest `r` (the market and the reference there), then the market moves to `r` through the
    ///         vault's liquidity.
    function _state(int24 lo, int24 r) internal {
        int24 p = r < lo + 10 ? lo + 10 : (r > lo + W - 10 ? lo + W - 10 : r);
        if (p != market) _moveMarket(p);
        _honestPlace(lo);
        if (r != market) _moveMarket(r);
    }

    /// @notice The curator's shares: every bank bucket it owns or may recover (cohorts, matured, vested, bond, the
    ///         pending reserve) plus the shares the bundler and its owner hold.
    function _curatorShares() internal view returns (uint256) {
        (uint128 coh, uint128 mp, uint128 vu, uint128 bond,,,) = v.bank();
        return uint256(coh) + mp + vu + bond + v.reserveShares() + v.balanceOf(address(bundler)) + v.balanceOf(owner);
    }

    /// @notice The vault's NAV at `sp` (call after a prologue in this state, so accrued fees are in idle).
    function _nav(uint160 sp) internal view returns (uint256 nav) {
        (nav,) = ExecLedger.value(_h(), sp);
    }

    /// @notice The coalition's wealth at the market price: the Sybil's tokens plus the curator's shares.
    function _wealth() internal view returns (int256) {
        uint160 sp = _sqrt(market);
        uint256 s = LiquidityValue.val(token0.balanceOf(address(syb)), token1.balanceOf(address(syb)), sp);
        uint256 c = FullMath.mulDiv(_curatorShares(), _nav(sp), v.totalSupply());
        return int256(s) + int256(c);
    }

    /// @notice The Sybil's tokens at the market price.
    function _sybilValue() internal view returns (int256) {
        return int256(LiquidityValue.val(token0.balanceOf(address(syb)), token1.balanceOf(address(syb)), _sqrt(market)));
    }

    /// @notice A curator call through the bundler; returns (reverted, selector).
    function _try(bytes memory call) internal returns (bool reverted, bytes4 err) {
        vm.prank(owner);
        (bool ok, bytes memory ret) = address(bundler).call(call);
        if (ok) return (false, bytes4(0));
        if (ret.length >= 4) err = bytes4(ret);
        return (true, err);
    }

    function _callPlace(int24 lo, PreSwap memory ps, uint160 moveTo) internal pure returns (bytes memory) {
        return abi.encodeCall(CuratorBundler.setPositions, (lo, lo + 2000, ps, moveTo, type(uint256).max));
    }

    function _callSwap(bool z, uint128 amt) internal pure returns (bytes memory) {
        return abi.encodeCall(CuratorBundler.rebalanceSwap, (z, amt, type(uint256).max));
    }

    /// @notice The pending action's charge and reserve (call between the action and its settlement).
    function _pendingCharge() internal view returns (uint256 c0, uint256 reserve) {
        (,,, uint128 c, uint128 r) = v.pending();
        return (c, r);
    }

    /// @notice `_settleDue` that returns the settlement's extra charge (the `Settled` event; 0 when nothing settled).
    function _settleDueExtra() internal returns (uint256 extra) {
        vm.recordLogs();
        _settleDue();
        Vm.Log[] memory logs = vm.getRecordedLogs();
        for (uint256 i; i < logs.length; ++i) {
            uint256 e = _extraOf(logs[i]);
            if (e > 0) extra = e;
        }
    }

    function _extraOf(Vm.Log memory l) private view returns (uint256 extra) {
        if (l.emitter != vault || l.topics[0] != keccak256("Settled(uint256,uint256,int24,bool)")) return 0;
        (, extra,,) = abi.decode(l.data, (uint256, uint256, int24, bool));
    }

    /// @notice The scripted reference follows the attacker: it has read `t` for the whole TWAP window and keeps reading
    ///         it through the markout window (the reference of a vault-dominated pool, whose price the attacker holds:
    ///         the thin-pool premise every positive control of a pricing defence switches on).
    function _refFollows(int24 t) internal {
        uint32[] memory st = new uint32[](1);
        int24[] memory tk = new int24[](1);
        (st[0], tk[0]) = (uint32(vm.getBlockTimestamp() - 2 hours), t);
        refPool.setPath(st, tk);
    }

    /// @notice The scripted reference back at the market (after a control's settlement).
    function _refAtMarket() internal {
        _refFollows(market);
    }

    /// @dev An optional JIT position the Sybil holds around the next `_attackPlace` (0: none).
    int24 internal jitLo;
    int24 internal jitHi;
    uint256 internal jitL;

    /// @notice One placement attack (with the JIT position `jitL` on `[jitLo, jitHi)` around the action, if set): the Sybil pushes spot to `pushTarget` (0: no push), the curator places on
    ///         `[lo, lo + W)` through the bundler, the Sybil unwinds spot to the market, the markout settles; the
    ///         coalition's net at the market price.
    function _attackPlace(uint160 pushTarget, int24 lo, PreSwap memory ps, uint160 moveTo) internal returns (Out memory o) {
        v.settle();
        int256 w0 = _wealth();
        int256 s0 = _sybilValue();
        uint256 c0 = _curatorShares();
        if (pushTarget != 0) syb.pushTo(pushTarget);
        (int24 jlo, int24 jhi, uint256 J) = (jitLo, jitHi, jitL);
        if (J > 0) syb.lp(jlo, jhi, int256(J));
        (o.reverted, o.err) = _try(_callPlace(lo, ps, moveTo));
        if (!o.reverted) (o.charge0,) = _pendingCharge();
        if (J > 0) syb.lp(jlo, jhi, -int256(J));
        syb.pushTo(_sqrt(market));
        o.extra = _settleDueExtra();
        _close(o, w0, s0, c0);
    }

    /// @notice Fills the outcome's measures against the state at the start (`w0`, `s0`, `c0`).
    function _close(Out memory o, int256 w0, int256 s0, uint256 c0) internal view {
        o.wealth = _wealth() - w0;
        o.sybil = _sybilValue() - s0;
        o.net = o.sybil - _burnedValue(c0, _curatorShares());
    }

    /// @notice The value at the market price of the curator's shares burned between `before` and `after_` (negative
    ///         when the curator's shares grew).
    function _burnedValue(uint256 before, uint256 after_) internal view returns (int256) {
        uint256 nav = _nav(_sqrt(market));
        uint256 S = v.totalSupply();
        if (before >= after_) return int256(FullMath.mulDiv(before - after_, nav, S));
        return -int256(FullMath.mulDiv(after_ - before, nav, S));
    }

    /// @notice One swap attack: the Sybil pushes spot to `pushTarget` (0: none) and adds JIT liquidity `J` on
    ///         `[jlo, jhi)` (0: none); the curator's `rebalanceSwap`; the JIT is removed, spot unwound, the markout
    ///         settled.
    function _attackSwap(uint160 pushTarget, int24 jlo, int24 jhi, uint256 J, bool z, uint128 amt)
        internal
        returns (Out memory o)
    {
        v.settle();
        int256 w0 = _wealth();
        int256 s0 = _sybilValue();
        uint256 c0 = _curatorShares();
        if (pushTarget != 0) syb.pushTo(pushTarget);
        if (J > 0) syb.lp(jlo, jhi, int256(J));
        (o.reverted, o.err) = _try(_callSwap(z, amt));
        if (!o.reverted) (o.charge0,) = _pendingCharge();
        if (J > 0) syb.lp(jlo, jhi, -int256(J));
        syb.pushTo(_sqrt(market));
        o.extra = _settleDueExtra();
        _close(o, w0, s0, c0);
    }

    uint256[5] internal KS = [uint256(0), 0.05e18, 0.3e18, 1e18, 3e18];

    /// @dev The legal `lo` nearest `loSeed` for a placement containing spot `s` and the reference `r`.
    function _legalLo(int24 s, int24 r, uint256 loSeed) internal view returns (int24) {
        int24 hiT = s > r ? s : r;
        int24 loT = s < r ? s : r;
        int24 loMin = _floor(hiT) + TICK_SPACING - W;
        int24 loMax = _floor(loT);
        uint256 n = uint256(int256((loMax - loMin) / TICK_SPACING)) + 1;
        return loMin + int24(int256(loSeed % n)) * TICK_SPACING;
    }

    /// @dev The legal `lo` that centres the range between spot `s` and the reference `r`.
    function _midLo(int24 s, int24 r) internal view returns (int24) {
        int24 loMin = _floor(s > r ? s : r) + TICK_SPACING - W;
        int24 loMax = _floor(s < r ? s : r);
        return loMin + _floor((loMax - loMin) / 2);
    }

    /// @dev State 0: in range (the deployment's placement at the market); 1 and 2: exit states holding both tokens,
    ///      built as they arise: an honest placement with spot 30 ticks inside one edge leaves the other token idle,
    ///      then the market leaves the range by 150 ticks on that side (1: above, 2: below).
    function _pickState(uint256 st) internal {
        int24 c = _floor(market);
        if (st % 3 == 1) {
            _honestPlace(c + 30 - W);
            _moveMarket(c + 30 + 150);
        } else if (st % 3 == 2) {
            _honestPlace(c - 30);
            _moveMarket(c - 30 - 150);
        }
    }

    /// @dev The edge targets for a push of `d` (Python's `n4edge.edge_targets`): spot one unit inside the lower edge
    ///      `e = floor(r + d)` (range `[e, e + W)`), or one unit inside the upper edge `e + SPACING` (range
    ///      `[e + SPACING − W, e + SPACING)`).
    function _edge(int24 d, bool upper) internal view returns (uint160 ps, int24 lo) {
        int24 e = _floor(market + d);
        if (!upper) return (_sqrt(e) + 1, e);
        return (_sqrt(e + TICK_SPACING) - 1, e + TICK_SPACING - W);
    }
    /// @notice The pass rule of every attack row: the coalition nets at most 1 wei per action.
    function _assertLoses(Out memory o, string memory why) internal pure {
        assertLe(o.net, 1, string.concat(why, ": the attack must lose money"));
        assertLe(o.wealth, 1, string.concat(why, ": the coalition's wealth must not grow"));
    }
}
