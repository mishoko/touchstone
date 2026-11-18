"""Shared simulator for the s2 cells: market paths, the 10-minute TWAP reference, and a full-model vault driven
hourly at the mainnet tier with modelled arbitrage.

Pinned touch set (the evaluation plan's replay knobs, applied to synthetic paths):
- the Touchstone pool's only liquidity is the vault's (third-party depth 0);
- an arbitrageur trades the pool toward the fee-adjusted market price at every hour mark when its gain exceeds
  its gas (gas at `GAS_GWEI`); its swap is a touch (the hook's beforeSwap snapshot runs first);
- beyond the vault's range the pool is empty, so arbitrage stops at the vault's edge and never touches an
  out-of-range vault;
- the bot's own calls (actions, pokes, keep-alive pings) are touches; the "observer" variant adds a third party
  calling refreshRef() every 2·GRID.

The market is a 5-minute log-price path (ticks); the reference read with end time `e` is the floor of the
trapezoid average of the path over [e − 600 s, e] (the 10-minute TWAP). Every event sits on an hour mark, which
is also a reference anchor (GRID_SEC = 3,600 s, GRID = 300 blocks of 12 s).

The vault, hook, pool, placement and charge logic are the reference model's (`model/reference.py`); nothing is
re-implemented here. The charge is the specification's markout-settled charge (the pre-move spot leg, the fixed
reserve, settlement against the reference pool's TWAP over the window after the action): the reference pool's
closing tick per 12-second block follows the market inside its arbitrage band (u_k = clip(u_{k-1}, m_k ± FEE_TICKS),
the market between the 5-minute path points a seeded Brownian bridge, the action block's close uniform in the band),
and the markout read is the floor of the mean close over the window. The curator's bundler re-aligns spot to the
reference pool's price before each action (s0 ≈ that price) and calls `settle()` once the window has closed (its
gas is charged). The bond is `BOND_RATIO`·S from seed and erodes with every bond burn; the solvency gate is
enforced (an `Insolvent` action reverts and is counted). Pay is valued with a measurement wrapper around `fee_bank_apply` that records, in a dict
carried on the Bank (so a reverted call rolls it back), the value of shares when they vest, are forfeited or are
burned from the bond, at the vault's share price at the market price just before the call.
"""
import copy
import math
import os
import random
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
from common import R, sqrt_at, T0, W, SPACING, floor_sp, ETH  # noqa: E402

STEP_SEC = 300                     # market path resolution (5 minutes)
STEPS_PER_HOUR = 12
REF_WINDOW = 600
GAS_GWEI = 5                       # assumed p50 base fee (the replay window's p50 replaces it in Track B)
GAS = dict(poke=150_000, ping=100_000, place=450_000, move=120_000, swap=300_000, arb=120_000, park=90_000, settle=150_000)
BLOCK_SEC = 12
YEAR_H = 365 * 24
FEE = 500
FEE_TICKS = 5


def gas_eth(kind, n=1):
    return GAS[kind] * n * GAS_GWEI * 1e-9


# ----------------------------------------------------------------------------- market paths
def path_key(seed, sigma, kind="gbm"):
    """The market path's RNG key (one place: `uic.py` checks fresh seeds against it)."""
    return seed * 7919 + int(sigma * 1000) + {"gbm": 0, "t3": 1, "hover": 2}[kind] * 104729


def market_path(seed, sigma, days, kind="gbm", step_sec=STEP_SEC, start=T0, edge=None):
    """Log-price path in ticks at `step_sec` resolution, with two pre-history points at `start` so the TWAP
    at t = 0 is defined. kinds: gbm (driftless), t3 (Student-t ν = 3 increments scaled to the same σ),
    hover (Ornstein-Uhlenbeck around `edge`, half-life 12 h, stationary sd ≈ 1.5× the hourly GBM sd × √12)."""
    kappa = 0.0
    rng = random.Random(path_key(seed, sigma, kind))
    n = days * 86400 // step_sec
    sd = sigma * math.sqrt(step_sec / (365 * 86400)) / math.log(1.0001)
    out = [float(start)] * 2
    x = float(start)
    if kind == "hover":
        kappa = math.log(2) / (12 * 3600 / step_sec)   # starts at `start`, pulled toward `edge`
    for _ in range(n + 3600 // step_sec + 1):  # one extra hour for the settlement poke
        if kind == "gbm":
            x += rng.gauss(0.0, sd)
        elif kind == "t3":
            z = rng.gauss(0.0, 1.0) / math.sqrt(rng.gammavariate(1.5, 2.0) / 3.0)  # t3, variance 3
            x += sd * z / math.sqrt(3.0)
        else:
            x += -kappa * (x - edge) + rng.gauss(0.0, sd)
        out.append(x)
    return out


def twap_tick(path, i, step_sec=STEP_SEC):
    """Floor of the 10-minute TWAP ending at path index i (trapezoid over the window)."""
    k = REF_WINDOW // step_sec
    pts = path[i - k:i + 1]
    avg = (sum(pts) - 0.5 * (pts[0] + pts[-1])) / k
    return math.floor(avg)


def path_at(path, x):
    """The path at a fractional index (linear between the 5-minute points)."""
    i = int(math.floor(x))
    f = x - i
    return path[i] if f == 0 else path[i] * (1 - f) + path[i + 1] * f


def twap_frac(path, x, step_sec=STEP_SEC):
    """Floor of the 10-minute TWAP ending at fractional index x (exact integral of the linear interpolant)."""
    if x == int(x):
        return twap_tick(path, int(x), step_sec)
    a = x - REF_WINDOW / step_sec
    pts = [a] + list(range(int(math.floor(a)) + 1, int(math.ceil(x)))) + [x]
    area = sum((pts[j + 1] - pts[j]) * (path_at(path, pts[j]) + path_at(path, pts[j + 1])) / 2 for j in range(len(pts) - 1))
    return math.floor(area / (REF_WINDOW / step_sec))


# ----------------------------------------------------------------------------- value shadow (measurement only)
_ORIG_APPLY = R.fee_bank_apply
CUR = {"pps": 0.0}
SETTLE_SPLIT = [0, 0, False]


def _shadowed_apply(b, op, args):
    if op == "Settle":   # the settlement burn's split (pay first, then bond) before the call clears the reserve
        x = min(args["burnS"], b.reserveShares)
        nb = min(x, b.reserveShares - b.reserveBond)
        SETTLE_SPLIT[:] = [nb, x - nb, b.reserveForfeit]
    r = _ORIG_APPLY(b, op, args)
    sh = b.__dict__.setdefault("shadow", dict(vested=0.0, bond=0.0, forfeit=0.0, minted=0.0, bank=0.0))
    if op in ("Accrue", "VestTime"):
        sh["vested"] += r[2] * CUR["pps"]
        if op == "Accrue":
            sh["minted"] += args["shares"] * CUR["pps"]
    elif op == "Charge":
        sh["bank"] += r[2] * CUR["pps"]
        sh["bond"] += r[3] * CUR["pps"]
    elif op == "Settle":
        sh["bank"] += SETTLE_SPLIT[0] * CUR["pps"]
        sh["bond"] += SETTLE_SPLIT[1] * CUR["pps"]
        if SETTLE_SPLIT[2]:
            sh["forfeit"] += r[3] * CUR["pps"]
    elif op in ("Breach", "Retire"):
        sh["forfeit"] += r[2] * CUR["pps"]
    return r


R.fee_bank_apply = _shadowed_apply
_ORIG_COLLECT = R.Pool.collect


def _measured_collect(pool, key, lo, hi):
    """Fees the vault collects, valued at the market (measurement; carried on the pool so a revert rolls it back)."""
    f0, f1 = _ORIG_COLLECT(pool, key, lo, hi)
    if key == R.Vault.KEY and (f0 or f1):
        pool.__dict__["fees_val"] = pool.__dict__.get("fees_val", 0.0) + (R.q(f0, CUR["ms"]) + f1) / 10 ** 18
    return f0, f1


R.Pool.collect = _measured_collect


# ----------------------------------------------------------------------------- the settlement rule and its shadow
# The model's settlement is the fee-aware one (spec §3.4; `R.settle_extra`; SETTLE["rule"] = "fee_aware"). For the
# comparison on the same paths the earlier holdings-only rule (legs tick_mk ± FEE_TICKS, extra = min(reserve, max(0,
# loss − charge₀)), burnS = min(reserveShares, ceilDiv(extra·S, NAV_now(p_mk))) at the holdings and supply at
# settlement) can be selected with SETTLE["rule"] = "holdings_only"; the holdings-only variant with the upper leg
# rounded up ("holdings_only_up1") is computed alongside. Every settlement records all three on the vault
# (`settle_log`), whichever rule is applied. SETTLE["int_closes"] (default off, for the cells gated before it existed)
# floors each block's close to an integer tick before the window mean is floored (the reference pool's closes are
# integer ticks per block, so `tryWindow` returns floor(mean_k floor(close_k))); off, the continuous mean is floored.
_ORIG_SETTLE = R.settle_extra
RULES = ("fee_aware", "holdings_only")
SETTLE = {"rule": "fee_aware", "vault": None, "int_closes": False}


def settle_holdings_only(before, after, tick_mk, fee_ticks, charge0, reserve, now_h, S, reserve_shares, up1):
    """The settlement before the fee-aware legs -> (extra, pMk, burnS)."""
    p_lo = R.get_sqrt_price_at_tick(max(R.MIN_TICK, min(R.MAX_TICK, tick_mk - fee_ticks)))
    p_hi = R.get_sqrt_price_at_tick(max(R.MIN_TICK, min(R.MAX_TICK, tick_mk + fee_ticks + (1 if up1 else 0))))
    l_lo = R.value(before, p_lo)[0] - R.value(after, p_lo)[0]
    l_hi = R.value(before, p_hi)[0] - R.value(after, p_hi)[0]
    loss_mk, p_mk = (l_hi, p_hi) if l_hi > l_lo else (l_lo, p_lo)
    extra = min(reserve, max(0, loss_mk - charge0))
    if not extra:
        return 0, p_mk, 0
    nav_now = R.value(now_h, p_mk)[0]
    if not nav_now:
        return extra, p_mk, reserve_shares
    return extra, p_mk, min(reserve_shares, R.ceil_div(extra * S, nav_now))


def _settle_shadow(before, after, pend, tick_mk, fee_ticks, fee_pips, reserve_shares):
    v = SETTLE["vault"]
    assert SETTLE["rule"] in RULES, SETTLE["rule"]
    r_fa = _ORIG_SETTLE(before, after, pend, tick_mk, fee_ticks, fee_pips, reserve_shares)
    r_ho = settle_holdings_only(before, after, tick_mk, fee_ticks, pend["charge0"], pend["reserve"], v.h, v.S,
                                reserve_shares, False)
    r_ho1 = settle_holdings_only(before, after, tick_mk, fee_ticks, pend["charge0"], pend["reserve"], v.h, v.S,
                                 reserve_shares, True)
    v.__dict__.setdefault("settle_log", []).append(
        dict(x_fa=r_fa[0], b_fa=r_fa[2], x_ho=r_ho[0], b_ho=r_ho[2], x_ho1=r_ho1[0], b_ho1=r_ho1[2], sAct=pend["sAct"],
             S=v.S, rs=reserve_shares))
    return r_fa if SETTLE["rule"] == "fee_aware" else r_ho


R.settle_extra = _settle_shadow


def tier_with_m(m_year, base=R.MAINNET):
    t = copy.copy(base)
    t.m_perBlock = R._m_per_block(base.BLOCKTIME, int(round(m_year * R.WAD)))
    return t


# ----------------------------------------------------------------------------- the world
class Sim:
    B0 = 50400 * 10          # epoch-aligned start block (a multiple of GRID)

    def __init__(self, path, m_year, nav_eth=1000, observer=False, place=True, tier=None, charge_rule="spec",
                 sigma=None, rng_seed=0, bond_ratio=None):
        self.t = tier or tier_with_m(m_year)
        self.charge_rule = charge_rule
        self.path = path
        self.ts0 = self.B0 * self.t.BLOCKTIME
        self.ref = R.RefSource(self._ref_at, window=self._window_at)
        self.sigma = sigma
        self.wrng = random.Random(rng_seed * 1_000_003 + 17)
        pool = R.Pool(sqrt_at(T0), FEE)
        self.v = R.Vault(self.t, self.ref, pool, sqrt_ref_price=sqrt_at(T0))
        s = sqrt_at(T0)
        a1 = nav_eth * ETH // 2
        a0 = R.mul_div(a1, 1 << 192, s * s)
        self.now = self.B0
        self.observer = observer
        self.last_obs = self.B0
        self.gas = 0.0
        self.trade = 0.0            # the curator's own trading P&L (spot moves), valued at the market
        self.counts = dict(actions=0, pokes=0, pings=0, moves=0, reverts=0, arbs=0, settles=0, fallbacks=0)
        self.extras = []            # markout extras at settlement (fraction of NAV), measurement only
        self.settle_rows = []       # per settlement: the three rules' extra (fraction of NAV) and burn (fraction of S),
        self.pend_tag = None        # tagged with the settled action (kind, the vault's own move, out of range before it)
        self.reverts = {}
        self.in_actual = 0
        self.hours = 0
        self.action_charges = []
        self.arb_pnl = 0            # the arbitrageur's realised gain (wei, at the market), measurement only
        self.set_pps()
        assert charge_rule in ("spec", "sq"), charge_rule
        if charge_rule == "sq":     # the status-quo control: worse of {spot, reference}, no reserve, no markout
            self._install_sq()
        self.v.seed(a0, a1, now=self.B0, ts=self.ts())
        br = self.t.BOND_RATIO if bond_ratio is None else bond_ratio
        self.v.bank.bondShares = R.mul_div(self.v.S, br, R.WAD)   # the bond at BOND_RATIO·S; depositors hold the rest
        if place:
            self.pend_tag = dict(kind="seed", move=False, exit=False)
            lo = floor_sp(T0) - W // 2
            self.v.set_positions(lo, lo + W, R.Vault.NO_PRESWAP, self.B0, self.ts())
            self.settle_pending()

    def _install_sq(self):
        """The status-quo charge (control only): the worse of {spot, reference}, burned at once; no s0 leg, no
        reserve, no pending markout."""
        v = self.v

        def charge(before, s0, s_spot, s_ref, kind, max_charge, ts):
            c, p_star, nav_after, burn, ok = R.measure(before, v.h, s_spot, s_ref, v.S, v.bank.unvested(), v.bank.bondShares)
            if not ok:
                raise R.Revert("Insolvent")
            bb, _, bank_b, bond_b = R.fee_bank_apply(v.bank, "Charge", dict(burn=burn, burnR=0))
            v.S -= bb
            r = v.rec(v.epoch)
            r["charge"] += c
            v.events.append(("Action", kind, c, p_star, bank_b, bond_b))
            return c, p_star, burn
        v._charge = charge

    def _window_at(self, start, end):
        """The reference pool's TWAP over (start, end] (the markout read): the mean closing tick of the 12-second
        blocks starting at `start`, floored. Closes follow the market inside the arbitrage band from a close at the
        action block uniform in the band; the market between path points is a seeded Brownian bridge."""
        n = int(round((end - start) / BLOCK_SEC))
        t_a = start - BLOCK_SEC
        sd12 = (self.sigma or 0.6) * math.sqrt(BLOCK_SEC / (365 * 86400)) / math.log(1.0001)
        rng = random.Random(self.wrng.random())
        # market at 12-second steps from t_a, bridged between the 5-minute path points
        ts = [t_a + BLOCK_SEC * k for k in range(n + 1)]
        m = []
        seg = None
        for tk in ts:
            x = (tk - self.ts0) / STEP_SEC + 2
            i0 = int(math.floor(x))
            if seg is None or seg[0] != i0:
                k_steps = STEP_SEC // BLOCK_SEC
                w = [0.0]
                for _ in range(k_steps):
                    w.append(w[-1] + rng.gauss(0.0, sd12))
                bridge = [w[q] - (q / k_steps) * w[-1] for q in range(k_steps + 1)]
                seg = (i0, bridge)
            q = int(round((x - i0) * (STEP_SEC // BLOCK_SEC)))
            m.append(path_at(self.path, x) + seg[1][q])
        u = m[0] + rng.uniform(-FEE_TICKS, FEE_TICKS)
        closes = []
        for k in range(1, n + 1):
            u = min(max(u, m[k] - FEE_TICKS), m[k] + FEE_TICKS)
            closes.append(u)
        if SETTLE["int_closes"]:
            return sum(math.floor(c) for c in closes) // n
        return math.floor(sum(closes) / n)

    def settle_pending(self):
        """The curator's bundler calls settle() once the markout window has closed (gas charged)."""
        v = self.v
        if v.pending is None:
            return
        due = v.pending["tAction"] + 2 * self.t.BLOCKTIME + self.t.H_MARKOUT
        self.now = max(self.now, -(-due // self.t.BLOCKTIME))
        nav = v.nav(self.mkt_sqrt())
        ev0 = len(v.events)
        ok, _ = self.vcall("settle", gas="settle")
        if ok:
            self.counts["settles"] += 1
            for e in v.events[ev0:]:
                if e[0] == "Settled":
                    self.extras.append(e[2] / max(1, nav))
                    if e[4]:
                        self.counts["fallbacks"] += 1

    # time and prices
    def ts(self, b=None):
        return (self.now if b is None else b) * self.t.BLOCKTIME

    def idx(self, ts):
        d = ts - self.ts0
        return d // STEP_SEC + 2 if d % STEP_SEC == 0 else d / STEP_SEC + 2

    def _ref_at(self, end):
        return twap_frac(self.path, self.idx(end))

    def mkt(self):
        return path_at(self.path, self.idx(self.ts()))

    def mkt_sqrt(self):
        return sqrt_at(int(round(self.mkt())))

    def set_pps(self):
        v = self.v
        CUR["ms"] = self.mkt_sqrt()
        CUR["pps"] = v.nav(CUR["ms"]) / v.S / ETH if v.S else 0.0

    def live_ref(self):
        ok, t = self.v.hook.ref.read(self.ts(), 0)
        return t if ok else None

    # calls
    def _drain_settle_log(self, nav):
        """Moves the settlements of the last call (none if it reverted) into `settle_rows`, valued at the NAV at the
        market before the call and tagged with the action they settle (`pend_tag`)."""
        log = self.v.__dict__.get("settle_log")
        if not log:
            return
        tag = self.pend_tag or dict(kind="unknown", move=False, exit=False)
        for e in log:
            self.settle_rows.append(dict(
                x_fa=e["x_fa"] / max(1, nav), b_fa=e["b_fa"] / max(1, e["sAct"]), x_ho=e["x_ho"] / max(1, nav),
                b_ho=e["b_ho"] / max(1, e["S"]), x_ho1=e["x_ho1"] / max(1, nav), b_ho1=e["b_ho1"] / max(1, e["S"]),
                cap_fa=e["b_fa"] >= e["rs"] > 0, **tag))
        log.clear()

    def _action_tag(self, name, kw):
        """The class of a curator action, read before the call: placement or swap, with or without the vault's own
        move, and whether the market was outside the vault's range (an exit re-entry)."""
        h = self.v.h
        out = h.L == 0 or not (h.lo <= self.mkt() < h.hi)
        if name == "set_positions":
            return dict(kind="place", move=bool(kw.get("move_to")), exit=out)
        return dict(kind="swap", move=False, exit=out)

    def vcall(self, name, *a, gas=None, **kw):
        self.set_pps()
        SETTLE["vault"] = self.v
        nav = self.v.nav(CUR["ms"])
        tag = self._action_tag(name, kw) if name in ("set_positions", "rebalance_swap") else None
        try:
            r = getattr(self.v, name)(*a, now=self.now, ts=self.ts()) if name != "set_positions" else \
                self.v.set_positions(*a, self.now, self.ts(), **kw)
            self._drain_settle_log(nav)
            if tag is not None:
                self.pend_tag = tag
            return True, r
        except (R.Revert, R.MathRevert) as e:
            self._drain_settle_log(nav)
            err = getattr(e, "err", str(e))
            self.reverts[err] = self.reverts.get(err, 0) + 1
            self.counts["reverts"] += 1
            return False, err
        finally:
            if gas:
                self.gas += gas_eth(gas)

    def deep_tick(self):
        """The reference pool's current price as the bundler reads it: the market within its arbitrage band."""
        return int(round(self.mkt() + self.wrng.uniform(-FEE_TICKS, FEE_TICKS)))

    def align(self):
        """The bundler re-aligns spot to the reference pool's price inside the action's transaction (s0 ≈ market)."""
        self.move_spot(sqrt_at(self.deep_tick()))

    def align_outside(self):
        """Align only through a region without vault liquidity (in range, the arbitrage already holds spot at the
        market within its band). Returns True if spot moved."""
        h, pool = self.v.h, self.v.pool
        u = self.deep_tick()
        su = sqrt_at(u)
        if h.L > 0 and not (su <= sqrt_at(h.lo) and pool.s <= sqrt_at(h.lo) or su >= sqrt_at(h.hi) and pool.s >= sqrt_at(h.hi)):
            return False
        if su == pool.s:
            return False
        self.move_spot(su)
        return True

    def poke(self):
        self.counts["pokes"] += 1
        return self.vcall("poke", gas="poke")

    def ping(self):
        v = self.v
        self.counts["pings"] += 1
        self.gas += gas_eth("ping")
        v.hook.refresh_ref(self.now, self.ts(), v.pool.s, v.pool.tick)

    def touch_swap(self):
        """The hook's beforeSwap snapshot (the first touch of the block)."""
        v = self.v
        v.hook.snapshot(self.now, self.ts(), v.pool.s, v.pool.tick)

    def move_spot(self, sqrt_target):
        """The curator's bundled spot move (its own swap): valued at the market as the curator's trade P&L."""
        pool = self.v.pool
        if pool.s == sqrt_target:
            return
        self.touch_swap()
        ain, aout, zfo = pool.push_to(sqrt_target)
        ms = self.mkt_sqrt()
        if zfo:
            self.trade += (aout - R.q(ain, ms)) / ETH
        else:
            self.trade += (R.q(aout, ms) - ain) / ETH
        self.counts["moves"] += 1
        self.gas += gas_eth("move")

    # arbitrage
    def arb(self):
        v, pool = self.v, self.v.pool
        h = v.h
        if h.L == 0:
            return
        m = self.mkt()
        sa, sb = sqrt_at(h.lo), sqrt_at(h.hi)
        s = pool.s
        up_t, dn_t = math.floor(m) - FEE_TICKS, math.ceil(m) + FEE_TICKS
        ms = self.mkt_sqrt()
        gas_wei = int(gas_eth("arb") * ETH)
        if sqrt_at(up_t) > s and s < sb:          # buy token0 from the pool (price up)
            tgt = min(sqrt_at(up_t), sb)
            a = max(s, sa)
            if tgt <= a:
                return
            ain = R.get_amount1_delta(a, tgt, h.L, True)
            aout = R.get_amount0_delta(a, tgt, h.L, False)
            gain = R.q(aout, ms) - ain - R.mul_div_up(ain, FEE, 10 ** 6 - FEE)
            if gain > gas_wei:
                self.touch_swap()
                used, out = pool.swap(False, 1 << 200, tgt)
                self.arb_pnl += R.q(out, ms) - used
                self.counts["arbs"] += 1
        elif sqrt_at(dn_t) < s and s > sa:        # sell token0 to the pool (price down)
            tgt = max(sqrt_at(dn_t), sa)
            b = min(s, sb)
            if tgt >= b:
                return
            ain = R.get_amount0_delta(tgt, b, h.L, True)
            aout = R.get_amount1_delta(tgt, b, h.L, False)
            gain = aout - R.q(ain + R.mul_div_up(ain, FEE, 10 ** 6 - FEE), ms)
            if gain > gas_wei:
                self.touch_swap()
                used, out = pool.swap(True, 1 << 200, tgt)
                self.arb_pnl += out - R.q(used, ms)
                self.counts["arbs"] += 1

    # the loop
    def run(self, policy, hours):
        G = self.t.GRID
        for hr in range(1, hours + 1):
            self.now = self.B0 + hr * G
            policy.pre(self)
            self.arb()
            policy.post(self)
            if self.observer and self.now - self.last_obs >= 2 * G:
                self.v.hook.refresh_ref(self.now, self.ts(), self.v.pool.s, self.v.pool.tick)
                self.last_obs = self.now
            h, hk = self.v.h, self.v.hook
            for e in self.v.events:
                if e[0] == "Action":
                    self.action_charges.append(e[2] / ETH)
            hk.events.clear()          # append-only logs (never read by the logic): keep the per-call copy small
            hk.ts.clear()
            self.v.events.clear()
            if len(hk.frozen) > 16:
                for g in sorted(hk.frozen)[:-16]:
                    del hk.frozen[g]
            if h.L > 0 and self.v.bank.placedByCurator and h.lo <= self.mkt() < h.hi:
                self.in_actual += 1
            self.hours += 1
            self.settle_pending()      # after the hour's bookkeeping (the settlement block is 12 blocks later)
        # settlement: one final poke at the horizon for every bot (identical treatment)
        self.now = self.B0 + (hours + 1) * G
        self.poke()
        return self.result(policy)

    SETTLE_KEYS = ("x_fa", "b_fa", "x_ho", "b_ho", "x_ho1", "b_ho1")
    SETTLE_CLASSES = {  # the honest-cost row's classes of the settled action
        "all": lambda r: True,
        "place": lambda r: r["kind"] == "place",
        "place_move": lambda r: r["kind"] == "place" and r["move"],
        "place_nomove": lambda r: r["kind"] == "place" and not r["move"],
        "place_exit": lambda r: r["kind"] == "place" and r["exit"],
        "place_in": lambda r: r["kind"] == "place" and not r["exit"],
        "swap": lambda r: r["kind"] == "swap",
    }

    def _settle_stats(self):
        """Per class of settled action: the count and, per rule, the sum, max and count > 0 of the extra (fraction of
        NAV) and of the burn (fraction of the supply), and the reserve-capped count under the fee-aware rule."""
        out = {}
        for cls, f in self.SETTLE_CLASSES.items():
            rows = [r for r in self.settle_rows if f(r)]
            d = dict(n=len(rows), cap_fa=sum(1 for r in rows if r["cap_fa"]))
            for k in self.SETTLE_KEYS:
                xs = [r[k] for r in rows]
                d[k + "_sum"] = sum(xs)
                d[k + "_max"] = max(xs, default=0.0)
                d[k + "_pos"] = sum(1 for x in xs if x > 0)
            out[cls] = d
        return out

    def result(self, policy):
        v = self.v
        b = v.bank
        sh = b.__dict__.get("shadow", {})
        self.set_pps()
        elapsed = self.now - self.B0
        charges = sum(r["charge"] for r in v.records.values()) / ETH
        unvested_end = b.unvested() * CUR["pps"]
        net = sh.get("vested", 0.0) - sh.get("bond", 0.0) - self.gas + self.trade
        return dict(bot=policy.name, net=net, vested=sh.get("vested", 0.0), bond_burn=sh.get("bond", 0.0),
                    bank_burn=sh.get("bank", 0.0), forfeit=sh.get("forfeit", 0.0), minted=sh.get("minted", 0.0),
                    gas=self.gas, trade=self.trade, charges=charges, fees=v.pool.__dict__.get("fees_val", 0.0), unvested_end=unvested_end,
                    net_incl_unvested=net + unvested_end, f=v.hook.tauV / elapsed, f_actual=self.in_actual / self.hours,
                    breaches=b.breaches, mode=b.mode, action_charges=self.action_charges, nav_end=v.nav(self.mkt_sqrt()) / ETH,
                    extras=self.extras, settle_stats=self._settle_stats(), **self.counts,
                    revert_kinds=dict(self.reverts))
