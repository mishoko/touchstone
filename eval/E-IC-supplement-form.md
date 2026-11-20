# E-IC historical-price supplement: form

Committed before any data in its window was fetched. The code is `eval/eic_hist.py` at the same commit.

**Label (printed with every number):** one historical price series through the synthetic simulator, no third-party
depth, point estimates, not a replay and not E-IC's rule.

E-IC stays **NOT RUN**. This supplement gives no verdict and never changes E-IC's status or the claims count.

## Preconditions (each must hold before the fetch)

| Rule | State |
|---|---|
| The documentation and frontend fix reviews are closed | closed (documentation: session 28; frontend: session 30) |
| The fetch starts no later than 2026-11-25, else it never starts and nothing public mentions it | `fetch` refuses after that date |
| This form and its code are committed before the fetch | `fetch` and `run` refuse uncommitted changes to the form, `eval/eic_hist.py` or `model/` |
| One design review before the fetch, one fix review after the run | if the fix review does not finish, the result file, the README line and the page say "Unreviewed" |

## What it is

```
mainnet USDC/WETH 0.05% Swap logs  ──►  one price point per 25 blocks  ──►  shifted to start at T0
(0x88e6…5640, blocks below)             (last swap at or before it)          (log returns kept)
                                                                                    │
                    the window's p50 base fee ──► gas price                         ▼
                                                              model/cells/sim.py (the U-IC2 simulator)
                                                              every U-IC2 bot, m 2.2%/yr, NAV 1,850 and 185
                                                                                    │
                                                                                    ▼
                                                    eval/supplements/E-IC-historical.json (point estimates)
```

The simulator is unchanged. Only the price path is historical. The Touchstone pool still holds only the vault's
liquidity. Arbitrage, the reference pool's block closes inside its fee band and the markout window are modelled
exactly as in U-IC2.

## Window

| Item | Value |
|---|---|
| Window end block | 26,443,420 (the fork tests' pinned block; timestamp 1795022615) |
| Step | 25 blocks per point (the simulator's 5-minute step at 12 s per block) |
| Points | 51,855 (the synthetic path's length: 2 before t = 0, 51,840 for 180 days, 13 for the closing hour) |
| First point | block 25,147,070 = 26,443,420 − 25 × 51,854 |
| Simulated span | 180 days of 7,200 blocks (blocks 25,147,120 to 26,443,120); calendar time is recorded |
| Logs fetched | Swap events of `0x88e6…5640`, blocks 25,139,870 to 26,443,420 (one day of lookback for point 0) |
| Base fees fetched | `eth_feeHistory`, every block from 25,147,070 to 26,443,420 |

**Price rule.** Point j is the post-swap `sqrtPriceX96` of the last Swap log (highest block, then log index) at or
before block 25,147,070 + 25 j. Its tick is ln(sqrtP² / 2¹⁹²) / ln 1.0001 (not rounded). A point with no prior swap
in the fetched range stops the run.

**Shift.** The path is shifted by one constant so that point 2 (t = 0) equals the simulator's T0 = 196,250. A constant
tick shift rescales USDC units only; NAV, charges and gas are in WETH.

**Volatility.** The realised volatility of the 25-block log increments over the simulated span, annualised with the
measured seconds per step (from the first and last block timestamps), is recorded. The simulator uses it only for the sub-step noise inside a markout window.

**Gas.** The median base fee over every block in the window, in gwei, replaces the synthetic 5 gwei. No priority fee.

**Reference.** The simulator computes the reference TWAP from the sampled path (the 10-minute trapezoid average of the
points), not from the pool's own oracle. Points 0 and 1 are historical, so the reference at t = 0 can differ slightly
from T0; the difference and whether every seed placement succeeded are recorded.

## Settings (as U-IC2, except where marked)

| Setting | Value |
|---|---|
| Mimic | edge-12h (`EDGE_DELAY` 12 GRID, the mainnet tier) |
| Bots | edge-12h, edge60, the 13 U-IC2 gated bots (lazy, lazy-under-ladder, lazy-quiet, lazy-silent, lazy-park, lazy-timed, lazy-keepalive, dodge, breach-reset, idle, dust, stale, wide), and delayed-24h, centre, edge-3h, edge-6h |
| Environments | no observer and observer (a third party refreshes the reference every 2 GRID), per bot as in U-IC2 |
| m | 2.2%/yr only (U-IC2 also ran 0 and 1%) |
| NAV | **1,850 ETH (the tier A cap) and 185 ETH (cap / 10)**; U-IC2 used 1,000 |
| Bond | never binds (as U-IC2's gated runs) |
| Settlement | fee-aware, integer closes (U-IC2's frozen settlement) |
| Third-party depth | 0 |
| Gas price | the window's p50 base fee (U-IC2: 5 gwei) |
| Sim RNG key | 1508, outside every key seeds 0–1507 fed (checked in the selftest) |

## Outputs, per NAV, environment and bot

net, net including unvested pay, vested, bond and bank burns, forfeits, minted base, charges, gas, trading result,
pool fees (arbitrage swaps only: no third-party flow), credited in-range share f, actual in-range share, actions,
pokes, pings, moves, reverts and their kinds, arbitrage swaps, settlements, breaches, end mode, end NAV, unvested pay at
the end, and whether the seed placement succeeded.

**Comparisons (point estimates).** For each U-IC2 gated bot: net(edge-12h) − net(bot) and f(edge-12h) − f(bot), in
U-IC2's pairing (lazy-under-ladder with an observer, every other bot without). The list of bots that edge-12h does
not beat on this path is printed as it comes out.

**In-range uplift (input to spec §3.5's depositor ceiling, not the ceiling).** The actual in-range share of edge-12h
(the current mimic) and of edge60 (the mimic §3.5 names) minus that of the never-re-placing lazy bot, no observer, per
NAV. It is a share of time; its value to depositors is not measured here (no third-party flow), so it is not compared
with m and the public wording "no depositor ceiling measured" stands. The base fee's measured cost (minted value / NAV,
edge-12h) is reported beside it.

**Not computed:** E-IC's pass rule (one path cannot carry its per-bucket paired inference), confidence intervals,
the curator's 5th-percentile net at σ p90, the per-tier term sheet.

## Integrity and publication

- The fetch records its block ranges, endpoints, fetch time and the sha256 of each raw file; the derived path
  (`eval/supplements/E-IC-hist/path.json`) carries the raw files' manifest, and the result carries the path's sha256
  and the model's hash.
- Raw files stay out of the repository (`output/eic-hist/`) and are backed up; the derived path is tracked.
- Once the data is fetched, the result is published whichever way it comes out: the result file, one labelled line in
  README Results, and the same labelled line on the project page. The line is fixed now and printed by `run`
  (`public_line` in the result file), filled only with the run's numbers:

  > E-IC historical supplement (\<label\>): blocks 25,147,070 to 26,443,420; edge-12h net {x} / {y} ETH at NAV 1,850 /
  > 185; gated bots edge-12h does not beat: {list, or none}; E-IC stays NOT RUN. [Unreviewed.]

- The fetch checks every log's block range and address, and re-fetches every 50th chunk from the other endpoint (the
  counts must match; recorded in the manifest).
- `fetch` writes `output/eic-hist/STARTED` and refuses a second fetch; `run` refuses if the result file exists. A rerun
  needs the owner and a recorded diff.
- No parameter, bot, output or rule above changes after the fetch. A crash may be fixed only if the fix changes no
  number of a run that completed; the diff is recorded. A fetch that cannot finish within 1 h is recorded as not run.
