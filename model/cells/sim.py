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
re-implemented here. Pay is valued with a measurement wrapper around `fee_bank_apply` that records, in a dict
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
GAS = dict(poke=150_000, ping=100_000, place=450_000, move=120_000, swap=300_000, arb=120_000, park=90_000)
YEAR_H = 365 * 24
FEE = 500
FEE_TICKS = 5


def gas_eth(kind, n=1):
    return GAS[kind] * n * GAS_GWEI * 1e-9


# ----------------------------------------------------------------------------- market paths
def market_path(seed, sigma, days, kind="gbm", step_sec=STEP_SEC, start=T0, edge=None):
    """Log-price path in ticks at `step_sec` resolution, with two pre-history points at `start` so the TWAP
    at t = 0 is defined. kinds: gbm (driftless), t3 (Student-t ν = 3 increments scaled to the same σ),
    hover (Ornstein-Uhlenbeck around `edge`, half-life 12 h, stationary sd ≈ 1.5× the hourly GBM sd × √12)."""
    kappa = 0.0
    rng = random.Random(seed * 7919 + int(sigma * 1000) + {"gbm": 0, "t3": 1, "hover": 2}[kind] * 104729)
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


# ----------------------------------------------------------------------------- value shadow (measurement only)
_ORIG_APPLY = R.fee_bank_apply
CUR = {"pps": 0.0}


def _shadowed_apply(b, op, args):
    r = _ORIG_APPLY(b, op, args)
    sh = b.__dict__.setdefault("shadow", dict(vested=0.0, bond=0.0, forfeit=0.0, minted=0.0, bank=0.0))
    if op in ("Accrue", "VestTime"):
        sh["vested"] += r[2] * CUR["pps"]
        if op == "Accrue":
            sh["minted"] += args["shares"] * CUR["pps"]
    elif op == "Charge":
        sh["bank"] += r[2] * CUR["pps"]
        sh["bond"] += r[3] * CUR["pps"]
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


def tier_with_m(m_year, base=R.MAINNET):
    t = copy.copy(base)
    t.m_perBlock = R._m_per_block(base.BLOCKTIME, int(round(m_year * R.WAD)))
    return t


# ----------------------------------------------------------------------------- the world
class Sim:
    B0 = 50400 * 10          # epoch-aligned start block (a multiple of GRID)

    def __init__(self, path, m_year, nav_eth=1000, observer=False, place=True, tier=None, charge_rule="sq",
                 sigma=None, rng_seed=0):
        self.t = tier or tier_with_m(m_year)
        self.charge_rule = charge_rule
        self.path = path
        self.ts0 = self.B0 * self.t.BLOCKTIME
        self.ref = R.RefSource(self._ref_at)
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
        self.counts = dict(actions=0, pokes=0, pings=0, moves=0, reverts=0, arbs=0)
        self.reverts = {}
        self.in_actual = 0
        self.hours = 0
        self.action_charges = []
        self.arb_pnl = 0            # the arbitrageur's realised gain (wei, at the market), measurement only
        self.set_pps()
        if charge_rule == "markout":
            self._install_markout(sigma, rng_seed)
        self.v.seed(a0, a1, now=self.B0, ts=self.ts())
        if place:
            lo = floor_sp(T0) - W // 2
            self.v.set_positions(lo, lo + W, R.Vault.NO_PRESWAP, self.B0, self.ts())

    def _install_markout(self, sigma, rng_seed, horizon_sec=120, delay_blocks=1, clamp_ticks=40, bond_ratio=6e-4):
        """Candidate charge under study (the markout-settled charge): the worse-of legs gain the pre-move spot s0 and
        the markout legs p_mk ± FEE_TICKS, where p_mk is the deep pool's TWAP over a window of `horizon_sec` that
        starts `delay_blocks` blocks after the action's block, clamped to spot ± clamp_ticks. s0: the honest
        curator re-aligns spot to the deep pool's price inside its own transaction, so s0 = the market tick plus the
        deep pool's arbitrage band (uniform ±FEE_TICKS). The post-action error of p_mk is drawn from the sim's seeded
        generator with the variance of the delayed window's average of 12-second increments and of the band; the
        settlement is applied at the action, and the curator's settlement call is charged as gas. The reserve (the
        most the markout legs can add over the clamp interval, scanned every 2 ticks) is checked against unvested +
        bond_ratio·S and failures are counted in `reverts['ReserveGate']` (reported; the action is not reverted)."""
        n = horizon_sec / 12
        sd12 = sigma * math.sqrt(12 / (365 * 86400)) / math.log(1.0001)
        sd = math.sqrt(sd12 ** 2 * (delay_blocks + n / 3) + FEE_TICKS ** 2 / (3 * n))
        rng = random.Random(rng_seed)
        v, sim = self.v, self

        def loss_at(before, p):
            na = R.value(v.h, p)[0]
            return R.value(before, p)[0] - na, na

        def charge(before, s_spot, s_ref, kind):
            m = sim.mkt()
            s0 = sqrt_at(int(round(m + rng.uniform(-FEE_TICKS, FEE_TICKS))))
            tick_spot = R.get_tick_at_sqrt_price(s_spot)
            mk = int(round(min(max(m + rng.gauss(0.0, sd), tick_spot - clamp_ticks), tick_spot + clamp_ticks)))
            loss, p_star, nav_after = 0, s_spot, R.value(v.h, s_spot)[0]
            imm = 0
            for i, p in enumerate([s_spot, s_ref, s0, sqrt_at(mk - FEE_TICKS), sqrt_at(mk + FEE_TICKS)]):
                l, na = loss_at(before, p)
                if i == 2:
                    imm = max(0, loss)
                if l > loss:                     # first leg (spot) wins ties
                    loss, p_star, nav_after = l, p, na
            imm = max(imm, loss_at(before, s0)[0], 0)
            res = max([loss_at(before, sqrt_at(tick_spot + d))[0] for d in
                       range(-clamp_ticks - FEE_TICKS, clamp_ticks + FEE_TICKS + 1, 2)] + [0]) - imm
            need = R.ceil_div((imm + max(0, res)) * v.S, max(1, nav_after))
            if need > v.bank.unvested() + int(bond_ratio * v.S):
                sim.reverts['ReserveGate'] = sim.reverts.get('ReserveGate', 0) + 1
            burn = 0 if loss == 0 else (R.U256 if nav_after == 0 else R.ceil_div(loss * v.S, nav_after))
            if burn > v.bank.unvested() + v.bank.bondShares:
                raise R.Revert("Insolvent")
            bb, _, bank_b, bond_b = R.fee_bank_apply(v.bank, "Charge", dict(burn=burn))
            v.S -= bb
            r = v.rec(v.epoch)
            r["charge"] += loss
            r["bankBurned"] += bank_b
            r["bondBurned"] += bond_b
            v.events.append(("Action", kind, loss, p_star, bank_b, bond_b))
            sim.gas += gas_eth("poke")           # the curator's settlement call after the window
            return loss, p_star, burn
        v._charge = charge

    # time and prices
    def ts(self, b=None):
        return (self.now if b is None else b) * self.t.BLOCKTIME

    def idx(self, ts):
        d = ts - self.ts0
        assert d % STEP_SEC == 0, "reads are aligned to the path grid"
        return d // STEP_SEC + 2

    def _ref_at(self, end):
        return twap_tick(self.path, self.idx(end))

    def mkt(self):
        return self.path[self.idx(self.ts())]

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
    def vcall(self, name, *a, gas=None):
        self.set_pps()
        try:
            r = getattr(self.v, name)(*a, now=self.now, ts=self.ts()) if name != "set_positions" else \
                self.v.set_positions(*a, self.now, self.ts())
            return True, r
        except (R.Revert, R.MathRevert) as e:
            err = getattr(e, "err", str(e))
            self.reverts[err] = self.reverts.get(err, 0) + 1
            self.counts["reverts"] += 1
            return False, err
        finally:
            if gas:
                self.gas += gas_eth(gas)

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
        # settlement: one final poke at the horizon for every bot (identical treatment)
        self.now += G
        self.poke()
        return self.result(policy)

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
                    breaches=b.breaches, mode=b.mode, action_charges=self.action_charges, nav_end=v.nav(self.mkt_sqrt()) / ETH, **self.counts,
                    revert_kinds=dict(self.reverts))
