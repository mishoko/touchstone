#!/usr/bin/env python3
"""E-IC historical-price supplement (form: eval/E-IC-supplement-form.md). One historical price series through the
synthetic simulator, no third-party depth, point estimates, not a replay and not E-IC's rule.

  python3 eval/eic_hist.py fetch      # Swap logs + base fees for the form's block range -> output/eic-hist/
  python3 eval/eic_hist.py derive     # raw files -> eval/supplements/E-IC-hist/path.json (+ MANIFEST.json)
  python3 eval/eic_hist.py run        # path -> eval/supplements/E-IC-historical.json
  python3 eval/eic_hist.py selftest   # the sampling, shift and sigma rules on a tiny synthetic log

stdlib only. The fetch sends standard JSON-RPC calls (eth_getLogs, eth_feeHistory, eth_getBlockByNumber, eth_call) to
public endpoints; no keys.
"""
import csv
import hashlib
import json
import math
import multiprocessing as mp
import os
import statistics
import sys
import time
import urllib.error
import urllib.request

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, os.path.join(ROOT, "model", "cells"))

# ----------------------------------------------------------------------------- the form (frozen before any fetch)
END_BLOCK = 26_443_420             # window end (the fork tests' pinned block, FORK_BLOCK_1)
STEP_BLOCKS = 25                   # one path point per 25 blocks = the simulator's 5-minute step at 12 s per block
N_POINTS = 51_855                  # = the simulator's synthetic path length for 180 days (2 + 51,840 + 13)
START_BLOCK = END_BLOCK - STEP_BLOCKS * (N_POINTS - 1)   # path point 0
LOOKBACK = 7_200                   # logs fetched from START_BLOCK - LOOKBACK so point 0 has a prior swap
DAYS = 180
M_YEAR = 0.022
NAVS = (1850, 185)                 # NAV_MAX (tier A) and NAV_MAX / 10, in ETH
RNG_SEED = 1508                    # the Sim's RNG key (markout-window bridge); outside every key seeds 0-1507 fed
DEADLINE = "2026-11-25"            # D3: the fetch starts no later than this date, else never
FORM = os.path.join(HERE, "E-IC-supplement-form.md")
MIMIC = "edge-12h"
LABEL = ("one historical price series through the synthetic simulator, no third-party depth, point estimates, "
         "not a replay and not E-IC's rule")
POOL = "0x88e6A0c2dDD26FEEb64F039a2c41296FcB3f5640"
SWAP_TOPIC = "0xc42079f94a6350d7e6235f29174924f928cc2ac818eb64fed8004e115fbcca67"
LOG_ENDPOINTS = ["https://rpc.mevblocker.io", "https://rpc.flashbots.net"]
FEE_ENDPOINTS = ["https://rpc.mevblocker.io", "https://ethereum-rpc.publicnode.com", "https://rpc.flashbots.net"]
RAW = os.path.join(ROOT, "output", "eic-hist")
SUPP = os.path.join(ROOT, "eval", "supplements", "E-IC-hist")
OUT_JSON = os.path.join(ROOT, "eval", "supplements", "E-IC-historical.json")
LN_TICK = math.log(1.0001)
STARTED = os.path.join(RAW, "STARTED")
PERIODS_PER_YEAR = 365 * 86400 / (STEP_BLOCKS * 12)


def sha256(path):
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


# ----------------------------------------------------------------------------- freeze guards (S31ED-03)
def git(*a):
    import subprocess
    return subprocess.run(["git", *a], cwd=ROOT, capture_output=True, text=True, check=True).stdout.strip()


FROZEN_PATHS = ("model/", "eval/eic_hist.py", "eval/E-IC-supplement-form.md")


def frozen_meta():
    """Refuses an uncommitted form, script or model; returns the commit, the form's sha256 and the model hash."""
    from m2d_rate import model_hash
    dirty = git("status", "--porcelain", "--", *FROZEN_PATHS)
    if dirty:
        sys.exit("uncommitted changes in the frozen paths (form, script, model):\n" + dirty)
    return dict(commit=git("rev-parse", "HEAD"), form_sha256=sha256(FORM), model_hash=model_hash())


# ----------------------------------------------------------------------------- fetch
class Client:
    def __init__(self, endpoints):
        self.endpoints, self.i, self.calls, self.retries, self.used = list(endpoints), 0, 0, 0, set()

    def call(self, method, params, tries=12):
        delay = 1.0
        for _ in range(tries):
            url = self.endpoints[self.i % len(self.endpoints)]
            try:
                self.calls += 1
                body = json.dumps({"jsonrpc": "2.0", "id": self.calls, "method": method, "params": params}).encode()
                req = urllib.request.Request(url, data=body, headers={"Content-Type": "application/json",
                                                                      "User-Agent": "eval-client/0.1"})
                with urllib.request.urlopen(req, timeout=90) as r:
                    resp = json.loads(r.read())
                if "error" in resp:
                    raise RuntimeError(str(resp["error"])[:160])
                self.used.add(url)
                return resp["result"]
            except (urllib.error.URLError, RuntimeError, TimeoutError, OSError, json.JSONDecodeError) as e:
                self.retries += 1
                print(f"  [{url}] {method} failed ({str(e)[:100]}); rotate, sleep {delay:.0f}s", file=sys.stderr)
                self.i += 1
                time.sleep(delay)
                delay = min(delay * 2, 30)
        raise RuntimeError(f"{method} failed after {tries} tries")


def _int256(h):
    v = int(h, 16)
    return v - (1 << 256) if v >= 1 << 255 else v


def fetch():
    if time.strftime("%Y-%m-%d") > DEADLINE:
        sys.exit(f"after {DEADLINE}: the supplement never starts (panel D3)")
    meta = frozen_meta()
    if os.path.exists(STARTED):
        sys.exit(f"{STARTED} exists: the fetch runs once; a restart needs the owner")
    os.makedirs(RAW, exist_ok=True)
    with open(STARTED, "w") as f:
        json.dump(meta | dict(started_unix=int(time.time())), f)
    t0 = time.time()
    checks = []
    lo, hi = START_BLOCK - LOOKBACK, END_BLOCK
    logs_c, fee_c = Client(LOG_ENDPOINTS), Client(FEE_ENDPOINTS)
    rows, b, chunk = [], lo, 2000
    while b <= hi:
        e = min(b + chunk - 1, hi)
        try:
            logs = logs_c.call("eth_getLogs", [{"address": POOL, "topics": [SWAP_TOPIC], "fromBlock": hex(b),
                                                "toBlock": hex(e)}], tries=6)
        except RuntimeError:
            if chunk > 100:
                chunk //= 2
                continue
            raise
        for lg in logs:
            assert b <= int(lg["blockNumber"], 16) <= e and lg["address"].lower() == POOL.lower(), lg  # S31ED-09
            if lg.get("removed"):
                continue
            d = lg["data"][2:]
            rows.append((int(lg["blockNumber"], 16), int(lg["logIndex"], 16), int(d[128:192], 16),
                         _int256(d[256:320])))
        if chunk == 2000 and (b - lo) // 2000 % 50 == 0:   # S31ED-09: every 50th chunk again from the other endpoint
            other = Client([u for u in LOG_ENDPOINTS if u != logs_c.endpoints[logs_c.i % len(LOG_ENDPOINTS)]])
            n2 = len(other.call("eth_getLogs", [{"address": POOL, "topics": [SWAP_TOPIC], "fromBlock": hex(b),
                                                  "toBlock": hex(e)}], tries=6))
            checks.append(dict(blocks=[b, e], n=len(logs), n_other=n2, endpoint_other=other.endpoints[0]))
            assert n2 == len(logs), checks[-1]
            print(f"  logs {b}-{e}: {len(rows)} swaps so far, {time.time() - t0:.0f}s", flush=True)
        b = e + 1
    rows.sort()
    with open(os.path.join(RAW, "swaps.csv"), "w", newline="") as f:
        w = csv.writer(f)
        w.writerow(("block", "log_index", "sqrtPriceX96", "tick"))
        w.writerows(rows)
    fees = []
    b = START_BLOCK
    while b <= END_BLOCK:
        n = min(1024, END_BLOCK - b + 1)
        r = fee_c.call("eth_feeHistory", [hex(n), hex(b + n - 1), []])
        assert int(r["oldestBlock"], 16) == b, (r["oldestBlock"], b)
        fees.extend((b + k, int(x, 16)) for k, x in enumerate(r["baseFeePerGas"][:n]))
        b += n
    with open(os.path.join(RAW, "basefees.csv"), "w", newline="") as f:
        w = csv.writer(f)
        w.writerow(("block", "baseFeePerGas"))
        w.writerows(fees)
    ts = {k: int(fee_c.call("eth_getBlockByNumber", [hex(k), False])["timestamp"], 16) for k in (START_BLOCK, END_BLOCK)}
    man = dict(frozen=meta, chunk_cross_checks=checks, pool=POOL, swap_topic0=SWAP_TOPIC, log_blocks=[lo, hi], fee_blocks=[START_BLOCK, END_BLOCK],
               timestamps={str(k): v for k, v in ts.items()}, fetched_at_unix=int(t0),
               fetch_s=round(time.time() - t0, 1), endpoints_used=sorted(logs_c.used | fee_c.used),
               rpc_calls=logs_c.calls + fee_c.calls, rpc_retries=logs_c.retries + fee_c.retries,
               files={n: dict(sha256=sha256(os.path.join(RAW, n)), bytes=os.path.getsize(os.path.join(RAW, n)),
                              rows=c) for n, c in (("swaps.csv", len(rows)), ("basefees.csv", len(fees)))})
    with open(os.path.join(RAW, "MANIFEST.json"), "w") as f:
        json.dump(man, f, indent=1)
    print(json.dumps(man, indent=1))


# ----------------------------------------------------------------------------- derive
def tick_of(sqrt_x96):
    return math.log(sqrt_x96 * sqrt_x96 / float(1 << 192)) / LN_TICK


def sample(swaps, start, step, n):
    """swaps: (block, log_index, sqrtPriceX96) sorted. Point j = the last swap (highest block, log index) with
    block <= start + step*j. Returns the sqrtPriceX96 per point; raises if point 0 has no prior swap."""
    out, k, last = [], 0, None
    for j in range(n):
        b = start + step * j
        while k < len(swaps) and swaps[k][0] <= b:
            last = swaps[k][2]
            k += 1
        if last is None:
            raise ValueError(f"no swap at or before block {b}")
        out.append(last)
    return out


def shift_and_sigma(ticks, t0, sec_per_step=STEP_BLOCKS * 12):
    """The path starts the simulation (index 2) at T0: a constant shift; sigma = realised annualised volatility of
    the per-step log-price increments over the simulated span (indices 2 .. 2 + 12*24*DAYS), annualised with the
    measured seconds per step (missed slots make it slightly above 300 s)."""
    shift = t0 - ticks[2]
    path = [x + shift for x in ticks]
    span = ticks[2: 2 + 12 * 24 * DAYS + 1]
    inc = [(span[i + 1] - span[i]) * LN_TICK for i in range(len(span) - 1)]
    return path, shift, statistics.pstdev(inc) * math.sqrt(365 * 86400 / sec_per_step)


def S_twap0(path):
    import sim as S
    return S.twap_tick(path, 2)


def derive():
    from common import T0
    man = json.load(open(os.path.join(RAW, "MANIFEST.json")))
    for n, d in man["files"].items():
        assert sha256(os.path.join(RAW, n)) == d["sha256"], f"{n}: sha256 differs from the fetch manifest"
    with open(os.path.join(RAW, "swaps.csv")) as f:
        r = csv.reader(f)
        next(r)
        swaps = [(int(a), int(b), int(c)) for a, b, c, _ in r]
    sq = sample(swaps, START_BLOCK, STEP_BLOCKS, N_POINTS)
    ticks = [tick_of(s) for s in sq]
    tsd = man["timestamps"]
    sec_per_step = (tsd[str(END_BLOCK)] - tsd[str(START_BLOCK)]) / (N_POINTS - 1)
    path, shift, sigma = shift_and_sigma(ticks, T0, sec_per_step)
    with open(os.path.join(RAW, "basefees.csv")) as f:
        r = csv.reader(f)
        next(r)
        fees = [int(x) for _, x in r]
    assert len(fees) == END_BLOCK - START_BLOCK + 1
    p50 = statistics.median(fees)
    os.makedirs(SUPP, exist_ok=True)
    doc = dict(label=LABEL, start_block=START_BLOCK, end_block=END_BLOCK, step_blocks=STEP_BLOCKS, n_points=N_POINTS,
               rule="point j = the post-swap sqrtPriceX96 of the last Swap log at or before block start + 25 j; "
                    "tick = ln(sqrtP^2 / 2^192) / ln 1.0001; path = tick + shift so that point 2 (t = 0) is T0",
               t0=T0, shift_ticks=shift, hist_tick_start=ticks[2], hist_tick_end=ticks[2 + 12 * 24 * DAYS],
               sigma_realised=sigma, sec_per_step=sec_per_step, twap_t0_minus_T0=S_twap0(path) - T0, basefee_p50_wei=p50, basefee_p50_gwei=p50 / 1e9,
               source_manifest=man, ticks=[round(x, 6) for x in path])
    with open(os.path.join(SUPP, "path.json"), "w") as f:
        json.dump(doc, f, separators=(",", ":"))
    with open(os.path.join(SUPP, "MANIFEST.json"), "w") as f:
        json.dump({k: v for k, v in doc.items() if k != "ticks"} | {"path_sha256": sha256(os.path.join(SUPP, "path.json"))},
                  f, indent=1)
    print(f"path: {len(path)} points, shift {shift:.3f} ticks, sigma {sigma:.4f}, p50 base fee {p50 / 1e9:.4f} gwei")


# ----------------------------------------------------------------------------- run
KEYS = ("net", "net_incl_unvested", "vested", "bond_burn", "bank_burn", "forfeit", "minted", "charges", "gas", "trade",
        "fees", "f", "f_actual", "actions", "pokes", "pings", "moves", "reverts", "arbs", "settles", "breaches", "mode",
        "nav_end", "unvested_end")


def _job(args):
    import sim as S
    import bots as B
    name, obs, nav, path, sigma, gwei = args
    S.SETTLE["rule"], S.SETTLE["int_closes"] = "fee_aware", True
    S.GAS_GWEI = gwei
    sim = S.Sim(path, M_YEAR, nav_eth=nav, observer=obs, place=B.PLACES.get(name, True), sigma=sigma,
                rng_seed=RNG_SEED, bond_ratio=10 ** 18)
    seed_placed = sim.v.h.L > 0 and bool(sim.v.bank.placedByCurator)
    reverts_h0 = dict(sim.reverts)
    r = sim.run(B.make(name), DAYS * 24)
    out = {k: r[k] for k in KEYS}
    out["mode"] = str(out["mode"])
    return dict(bot=name, observer=obs, nav=nav, seed_placed=seed_placed, reverts_h0=reverts_h0,
                revert_kinds=r["revert_kinds"], minted_frac_nav=r["minted"] / nav, **out)


def bot_set():
    import bots as B
    return tuple(dict.fromkeys((MIMIC, "edge60") + B.GATED + B.REPORTED))


PUBLIC_LINE = ("E-IC historical supplement ({label}): blocks {a:,} to {b:,}; edge-12h net {x:+.3f} / {y:+.3f} ETH "
               "at NAV 1,850 / 185; gated bots edge-12h does not beat: {below}; E-IC stays NOT RUN.{rev}")


def public_line(out, reviewed):
    n = out["by_nav"]
    below = sorted(set(n["1850"]["mimic_below"]) | set(n["185"]["mimic_below"]))
    return PUBLIC_LINE.format(label=LABEL, a=out["window"]["start_block"], b=out["window"]["end_block"],
                              x=n["1850"]["rows"]["no-observer"][MIMIC]["net"], y=n["185"]["rows"]["no-observer"][MIMIC]["net"],
                              below=", ".join(below) if below else "none", rev="" if reviewed else " Unreviewed.")


def run(procs=4, guard=True):
    import bots as B
    if guard:
        meta = frozen_meta()
        if os.path.exists(OUT_JSON):
            sys.exit(f"{OUT_JSON} exists: the supplement runs once; a rerun needs the owner and a recorded diff")
    else:
        meta = dict(commit=None, form_sha256=None, model_hash=None)
    doc = json.load(open(os.path.join(SUPP, "path.json")))
    path, sigma, gwei = doc["ticks"], doc["sigma_realised"], doc["basefee_p50_gwei"]
    assert len(path) == N_POINTS
    jobs = []
    for nav in NAVS:
        for name in bot_set():
            variants = (True,) if name == "lazy-under-ladder" else (False,) if name == "lazy" else (False, True)
            jobs += [(name, obs, nav, path, sigma, gwei) for obs in variants]
    t0 = time.time()
    with mp.Pool(procs) as pool:
        rows = pool.map(_job, jobs, chunksize=1)
    ix = {(r["nav"], r["observer"], r["bot"]): r for r in rows}
    per_nav = {}
    for nav in NAVS:
        mim = ix[(nav, False, MIMIC)]
        cmp = {}
        for b in B.GATED:
            env = b == "lazy-under-ladder"      # U-IC2's pairing: lazy-under-ladder exists only with an observer
            cmp[b] = dict(env="observer" if env else "no-observer",
                          net_diff=ix[(nav, env, MIMIC)]["net"] - ix[(nav, env, b)]["net"],
                          f_diff=ix[(nav, env, MIMIC)]["f"] - ix[(nav, env, b)]["f"])
        lazy = ix[(nav, False, "lazy")]
        e60 = ix[(nav, False, "edge60")]
        per_nav[str(nav)] = dict(
            mimic=MIMIC,
            rows={env: {r["bot"]: {k: r[k] for k in KEYS} for r in rows if r["nav"] == nav and r["observer"] == (env == "observer")}
                  for env in ("no-observer", "observer")},
            mimic_minus_bot=cmp,
            mimic_below=[b for b, c in cmp.items() if c["net_diff"] <= 0],
            in_range_uplift=dict(
                f_actual_lazy=lazy["f_actual"], f_actual_edge12h=mim["f_actual"], f_actual_edge60=e60["f_actual"],
                uplift_time_share_edge12h=mim["f_actual"] - lazy["f_actual"],
                uplift_time_share_edge60=e60["f_actual"] - lazy["f_actual"],
                base_minted_frac_nav_edge12h=mim["minted_frac_nav"],
                note="a share of time in range (no observer), not the depositor ceiling: its value to depositors is "
                     "not measured (no third-party flow; the only pool fees are arbitrage fees); no depositor "
                     "ceiling is measured"))
    out = dict(
        id="E-IC-historical", label=LABEL, verdict=None, e_ic_verdict="NOT_RUN",
        form="eval/E-IC-supplement-form.md",
        window=dict(start_block=doc["start_block"], end_block=doc["end_block"],
                    timestamps=doc["source_manifest"]["timestamps"], step_blocks=STEP_BLOCKS, n_points=N_POINTS),
        data=dict(path="eval/supplements/E-IC-hist/path.json", path_sha256=sha256(os.path.join(SUPP, "path.json")),
                  raw=doc["source_manifest"]["files"], fetched_at_unix=doc["source_manifest"]["fetched_at_unix"]),
        sigma_realised=sigma, sec_per_step=doc["sec_per_step"], twap_t0_minus_T0=doc["twap_t0_minus_T0"],
        basefee_p50_gwei=gwei, shift_ticks=doc["shift_ticks"],
        hist_ticks=dict(start=doc["hist_tick_start"], end=doc["hist_tick_end"]),
        settings=dict(m_year=M_YEAR, navs_eth=list(NAVS), mimic=MIMIC, bots=list(bot_set()), rng_seed=RNG_SEED,
                      settlement="fee_aware, integer closes", bond="never binds (bond_ratio = 1, as U-IC2)",
                      third_party_depth=0, observer_env="reported beside the no-observer environment"),
        by_nav=per_nav,
        not_computed=["E-IC's pass rule (one path: no per-bucket paired inference)", "confidence intervals",
                      "curator p5 net at sigma p90", "the per-tier term sheet"],
        seed_placed_all=all(r["seed_placed"] for r in rows if B.PLACES.get(r["bot"], True)),
        reviews=dict(design="internal record r1", fix=None),
        meta=meta | dict(wall_s=round(time.time() - t0, 1), sims=len(rows)))
    out["public_line"] = public_line(out, reviewed=False)
    with open(OUT_JSON, "w") as f:
        json.dump(out, f, indent=1, allow_nan=False)
    for nav in NAVS:
        p = per_nav[str(nav)]
        print(f"NAV {nav}: mimic net {ix[(nav, False, MIMIC)]['net']:.4f} ETH; mimic at or below: {p['mimic_below']}")
    print(out["public_line"])


def mark_reviewed(record):
    """After the one fix review (P-11a) closes: records it and drops " Unreviewed." from the public line."""
    out = json.load(open(OUT_JSON))
    out["reviews"]["fix"] = record
    out["public_line"] = public_line(out, reviewed=True)
    with open(OUT_JSON, "w") as f:
        json.dump(out, f, indent=1, allow_nan=False)
    print(out["public_line"])


# ----------------------------------------------------------------------------- selftest
def selftest():
    swaps = [(10, 0, 1 << 96), (10, 3, 2 << 96), (12, 1, 3 << 96), (30, 0, 4 << 96)]
    assert sample(swaps, 10, 5, 3) == [2 << 96, 3 << 96, 3 << 96]          # last log of a block wins; carries forward
    try:
        sample(swaps, 9, 5, 1)
        raise AssertionError("a point without a prior swap must raise")
    except ValueError:
        pass
    assert abs(tick_of(1 << 96)) < 1e-9 and abs(tick_of(int((1 << 96) * 1.0001 ** 0.5)) - 1) < 1e-6
    n = 2 + 12 * 24 * DAYS + 1
    ticks = [100.0 + (i % 2) for i in range(n)]
    path, shift, sig = shift_and_sigma(ticks, 196250)
    assert path[2] == 196250 and shift == 196250 - ticks[2]
    assert abs(sig - LN_TICK * math.sqrt(PERIODS_PER_YEAR)) < 1e-9          # |increment| = 1 tick every step
    assert START_BLOCK == 25_147_070 and START_BLOCK + STEP_BLOCKS * (N_POINTS - 1) == END_BLOCK
    assert N_POINTS == 2 + 12 * 24 * DAYS + 13                              # the synthetic path's length (sim.market_path)
    import uic
    assert not ({("sim", RNG_SEED)} & uic.stream_keys(range(0, 1508))), "RNG_SEED collides with a used key"
    print("eic_hist selftest OK")


if __name__ == "__main__":
    if sys.argv[1] == "mark-reviewed":
        mark_reviewed(sys.argv[2])
    else:
        {"fetch": fetch, "derive": derive, "run": run, "selftest": selftest}[sys.argv[1]]()
