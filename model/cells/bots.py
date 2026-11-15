"""Curator policies (bots) for cell (a), driven by `sim.Sim` at hourly marks.

Each policy implements pre(sim) (before the hour's arbitrage) and post(sim) (after it). Every policy except idle,
dust and wide starts from the same deploy-time placement (centred on the seed price, made by the bundler at
deployment).

Definitions (the spec's bot list, made executable; the conventions are logged in the decision log):
- edge60 (mimic): the reference policy. Acts when the live reference has been out of range for EDGE_DELAY (two
  consecutive hourly reads out) or the observed out-of-range clock reaches EDGE_DELAY (hover). One setPositions:
  classify the post-removal inventory at the live reference, place [c, c+W) (mostly token0) or [c'+spacing−W,
  c'+spacing) (mostly token1); if the fence fails or δ_after would be below DELTA_MIN at the current spot, first
  move spot (bundled, its own swap) one unit inside the inventory-side edge at the floored reference; if neither
  edge reaches DELTA_MIN (an inventory near 50/50), place the legal range containing spot and the reference that
  maximises δ_after. Keep-alive: refreshRef when now ≥ refCachedAt + 2·GRID − GRID/2 and, after a gap, at every
  anchor until it clears. Pokes at each epoch start.
- delayed-24h: edge60 with a 24-hour delay (both triggers).
- centre: after one hour out, tries the range centred on the reference; falls back to edge60's placement when
  that range fails DELTA_MIN (third-party depth is 0, so no pre-swap can fill). Reported only.
- lazy: never re-places; pokes once a day.
- lazy-under-ladder: lazy in the observer variant (a third party observes every 2·GRID, so the breach ladder
  applies to its absences).
- lazy-quiet: never re-places; pokes only at epoch starts.
- lazy-silent: never re-places, never pings; at most one poke per epoch, at the first hour at which its poke
  would find the gate open (fresh in-range cache, no gap, live reference in range); it never pokes while out.
- lazy-park: lazy-quiet that, at each exit, moves the pool price one unit inside the exited edge (parking), then
  stays silent.
- lazy-timed: silent while out; pokes at the first in-range anchor after a return, before any arbitrage; epoch
  pokes only while in range.
- lazy-keepalive: never re-places; keep-alive pings while the cache is in range; silent while out; pings at every
  anchor while a gap is open after a return; epoch pokes while in range.
- dodge: lazy that, when the observed clock reaches CURE − 3·GRID (a touch credits ≤ 2·GRID), makes the legal placement with the smallest
  δ_after ≥ DELTA_MIN, then idles again.
- breach-reset: lazy that, at the same trigger, makes edge60's (cheapest) placement, then idles again.
- idle: never places (EMPTY); pokes once a day. dust: tries a placement with only dust deployed (reverts
  DeltaMin), then idle. stale: never touches the vault after deployment. wide: tries a full-range placement
  (reverts Width), then idle.
"""
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
from common import R, sqrt_at, W, SPACING, floor_sp  # noqa: E402

DMIN = R.WAD // 2


def _inventory_after_removal(sim):
    v = sim.v
    h = v.h
    x, y = R.position_amounts(h.L, h.lo, h.hi, v.pool.s) if h.L else (0, 0)
    return R.Holdings(h.idle0 + x, h.idle1 + y)


def _delta_at(inv, lo, hi, s, sref):
    try:
        _, _, d = R.placement_size(inv, lo, hi, s, sref)
        return d
    except R.MathRevert:
        return 0


def _legal(lo, tick_spot, ref, band):
    return R.placement_check(lo, lo + W, tick_spot, ref, band, W, SPACING) == 0


def best_delta_range(sim, ref, want="max"):
    """The legal range (containing spot and ref, aligned, W wide) with the largest δ_after (want='max') or the
    smallest δ_after ≥ DELTA_MIN (want='min'), at the current spot."""
    v = sim.v
    inv = _inventory_after_removal(sim)
    spot = v.pool.tick
    sref = sqrt_at(ref)
    lo_min = floor_sp(max(spot, ref)) + SPACING - W
    lo_max = floor_sp(min(spot, ref))
    best = None
    for lo in range(lo_min, lo_max + 1, 50):
        for l2 in (lo, min(lo + 10, lo_max)):
            if not _legal(l2, spot, ref, sim.t.REF_BAND):
                continue
            d = _delta_at(inv, l2, l2 + W, v.pool.s, sref)
            if d < DMIN:
                continue
            key = d if want == "max" else -d
            if best is None or key > best[0]:
                best = (key, l2)
    return None if best is None else best[1]


def can_act(sim):
    """Curator actions are open in NORMAL and BREACHED only (a removed or retired curator stops acting)."""
    return sim.v.bank.mode != R.EXITED


def edge_placement(sim, ref):
    """edge60's placement. Returns True on success."""
    v = sim.v
    if not can_act(sim):
        return False
    pool = v.pool
    inv = _inventory_after_removal(sim)
    sref = sqrt_at(ref)
    spot = pool.tick
    band = sim.t.REF_BAND
    mostly0 = R.q(inv.idle0, sref) >= inv.idle1
    if mostly0:
        lo2 = floor_sp(min(ref, spot))
    else:
        lo2 = floor_sp(max(ref, spot)) + SPACING - W
    in_place = (abs(spot - ref) <= band and _legal(lo2, spot, ref, band)
                and _delta_at(inv, lo2, lo2 + W, pool.s, sref) >= DMIN + DMIN // 1000)
    if not in_place:
        c = floor_sp(ref)
        if mostly0:
            lo2, target = c, sqrt_at(c) + 1
        else:
            lo2, target = c + SPACING - W, sqrt_at(c + SPACING) - 1
        tgt_tick = R.get_tick_at_sqrt_price(target)
        if _delta_at(inv, lo2, lo2 + W, target, sref) >= DMIN + DMIN // 1000:
            sim.move_spot(target)
            assert pool.tick == tgt_tick
        else:
            lo_b = best_delta_range(sim, ref, "max")
            if lo_b is None:
                sim.counts["reverts"] += 1
                sim.reverts["NoLegalPlacement"] = sim.reverts.get("NoLegalPlacement", 0) + 1
                return False
            lo2 = lo_b
    ok, _ = sim.vcall("set_positions", lo2, lo2 + W, R.Vault.NO_PRESWAP, gas="place")
    if ok:
        sim.counts["actions"] += 1
    return ok


class Base:
    name = "base"
    observer_name = None

    def pre(self, sim):
        pass

    def post(self, sim):
        pass

    @staticmethod
    def epoch_start(sim):
        return sim.now % sim.t.EPOCH == 0


class Edge60(Base):
    name = "edge60"

    def __init__(self, delay_h=1, keepalive=True, name=None):
        self.delay_h = delay_h
        self.keepalive = keepalive
        self.out_h = 0
        if name:
            self.name = name

    def keep_alive(self, sim):
        hk = sim.v.hook
        G = sim.t.GRID
        if hk.emptyRange:
            return
        if sim.now >= hk.refCachedAt + 2 * G - G // 2 or hk.gapSinceVerify:
            sim.ping()

    def placement(self, sim, ref):
        return edge_placement(sim, ref)

    def post(self, sim):
        v, hk = sim.v, sim.v.hook
        ref = sim.live_ref()
        if ref is None:
            return
        out = hk.emptyRange or not (hk.vaultLo <= ref < hk.vaultHi)
        self.out_h = self.out_h + 1 if out else 0
        G = sim.t.GRID
        if can_act(sim) and (self.out_h > self.delay_h or hk.outBlocks >= self.delay_h * G):
            if self.placement(sim, ref):
                self.out_h = 0
        if self.keepalive:
            self.keep_alive(sim)
        if self.epoch_start(sim):
            sim.poke()


class Centre(Edge60):
    name = "centre"

    def placement(self, sim, ref):
        lo = floor_sp(ref) - W // 2
        v = sim.v
        if not can_act(sim):
            return False
        if _legal(lo, v.pool.tick, ref, sim.t.REF_BAND) and \
                _delta_at(_inventory_after_removal(sim), lo, lo + W, v.pool.s, sqrt_at(ref)) >= DMIN + DMIN // 1000:
            ok, _ = sim.vcall("set_positions", lo, lo + W, R.Vault.NO_PRESWAP, gas="place")
            if ok:
                sim.counts["actions"] += 1
                sim.counts["centred"] = sim.counts.get("centred", 0) + 1
            return ok
        return edge_placement(sim, ref)


class Lazy(Base):
    name = "lazy"
    every_h = 24

    def post(self, sim):
        if (sim.now // sim.t.GRID) % self.every_h == 0:
            sim.poke()


class LazyQuiet(Base):
    name = "lazy-quiet"

    def post(self, sim):
        if self.epoch_start(sim):
            sim.poke()


def gate_would_open(sim):
    hk = sim.v.hook
    ref = sim.live_ref()
    lo, hi = hk.vaultLo, hk.vaultHi
    return (ref is not None and not hk.emptyRange and hk.fresh(sim.now) and not hk.gapSinceVerify
            and lo <= hk.refTick < hi and lo <= ref < hi)


class LazySilent(Base):
    name = "lazy-silent"

    def __init__(self):
        self.poked_epoch = None

    def post(self, sim):
        e = sim.now // sim.t.EPOCH
        if self.poked_epoch != e and gate_would_open(sim):
            sim.poke()
            self.poked_epoch = e


class LazyPark(Base):
    name = "lazy-park"

    def __init__(self):
        self.parked_for = None

    def post(self, sim):
        v, hk = sim.v, sim.v.hook
        h = v.h
        m = sim.mkt()
        key = (h.lo, h.hi)
        if h.L and not (h.lo <= m < h.hi) and self.parked_for != key:
            target = sqrt_at(h.lo) + 1 if m < h.lo else sqrt_at(h.hi) - 1
            sim.move_spot(target)
            sim.gas += 0  # move gas already counted
            self.parked_for = key
        if h.L and h.lo <= m < h.hi:
            self.parked_for = None
        if self.epoch_start(sim):
            sim.poke()


class LazyTimed(Base):
    name = "lazy-timed"

    def __init__(self):
        self.was_out = False

    def pre(self, sim):
        hk = sim.v.hook
        ref = sim.live_ref()
        inr = ref is not None and hk.vaultLo <= ref < hk.vaultHi
        if inr and self.was_out:
            sim.poke()           # the first in-range anchor after a return, before any arbitrage
        self.was_out = not inr

    def post(self, sim):
        if not self.was_out and self.epoch_start(sim):
            sim.poke()


class LazyKeepalive(Base):
    name = "lazy-keepalive"

    def post(self, sim):
        hk = sim.v.hook
        ref = sim.live_ref()
        inr = ref is not None and hk.vaultLo <= ref < hk.vaultHi
        G = sim.t.GRID
        if inr and (sim.now >= hk.refCachedAt + 2 * G - G // 2 or hk.gapSinceVerify):
            sim.ping()
            if not hk.gapSinceVerify and (hk.pendingBreaches or sim.v.bank.maturedPending or
                                          any(s > 0 and sim.now // sim.t.EPOCH >= e + 1 + R.V
                                              for e, s in sim.v.bank.cohorts)):
                sim.poke()
        if inr and self.epoch_start(sim):
            sim.poke()


class Dodge(Lazy):
    name = "dodge"
    want = "min"

    def post(self, sim):
        super().post(sim)
        hk = sim.v.hook
        # a touch credits at most 2·GRID, so CURE − 3·GRID is the last hourly check that is always before the latch
        if can_act(sim) and hk.outBlocks >= sim.t.CURE_BLOCKS - 3 * sim.t.GRID:
            ref = sim.live_ref()
            if ref is None:
                return
            if self.want == "min":
                lo = best_delta_range(sim, ref, "min")
                if lo is None:
                    edge_placement(sim, ref)
                    return
                ok, _ = sim.vcall("set_positions", lo, lo + W, R.Vault.NO_PRESWAP, gas="place")
                if ok:
                    sim.counts["actions"] += 1
            else:
                edge_placement(sim, ref)


class BreachReset(Dodge):
    name = "breach-reset"
    want = "edge"


class Idle(Lazy):
    name = "idle"


class Dust(Lazy):
    name = "dust"

    def __init__(self):
        self.tried = False

    def post(self, sim):
        if not self.tried:
            self.tried = True
            v = sim.v
            ref = sim.live_ref()
            # deploy only a dust share: withdraw is not a curator path, so the only lever is the range; a range
            # whose spot sits at its far edge deploys the scarce token only (δ ≈ 0) and reverts DeltaMin
            lo = floor_sp(min(ref, v.pool.tick)) + SPACING - W
            sim.vcall("set_positions", lo, lo + W, R.Vault.NO_PRESWAP, gas="place")
        super().post(sim)


class Stale(Base):
    name = "stale"


class Wide(Lazy):
    name = "wide"

    def __init__(self):
        self.tried = False

    def post(self, sim):
        if not self.tried:
            self.tried = True
            ref = sim.live_ref()
            lo = floor_sp(ref) - 5 * W
            sim.vcall("set_positions", lo, lo + 10 * W, R.Vault.NO_PRESWAP, gas="place")
        super().post(sim)


def make(name):
    return {
        "edge60": lambda: Edge60(),
        "delayed-24h": lambda: Edge60(delay_h=23, name="delayed-24h"),  # acts at the 24th consecutive hourly read out
        "centre": Centre,
        "edge-3h": lambda: Edge60(delay_h=3, name="edge-3h"),
        "edge-6h": lambda: Edge60(delay_h=6, name="edge-6h"),
        "edge-12h": lambda: Edge60(delay_h=12, name="edge-12h"),
        "lazy": Lazy,
        "lazy-under-ladder": Lazy,
        "lazy-quiet": LazyQuiet,
        "lazy-silent": LazySilent,
        "lazy-park": LazyPark,
        "lazy-timed": LazyTimed,
        "lazy-keepalive": LazyKeepalive,
        "dodge": Dodge,
        "breach-reset": BreachReset,
        "idle": Idle,
        "dust": Dust,
        "stale": Stale,
        "wide": Wide,
    }[name]()


PLACES = {"idle": False, "dust": False, "wide": False}
LAZY_FAMILY = ("lazy", "lazy-under-ladder", "lazy-quiet", "lazy-silent", "lazy-park", "lazy-timed", "lazy-keepalive")
GATED = LAZY_FAMILY + ("dodge", "breach-reset", "idle", "dust", "stale", "wide")
REPORTED = ("delayed-24h", "centre", "edge-3h", "edge-6h", "edge-12h")  # edge-k: patient variants of the mimic
ALL = ("edge60",) + GATED + REPORTED
