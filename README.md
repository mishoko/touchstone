# Touchstone

A Uniswap v4 hook vault in which the pool keeps the curator's books.

When a curator re-positions a liquidity vault, the cost of that move (swap fees, price impact, value lost to a
friendly counterparty) normally comes out of depositors' money. Touchstone has the pool measure what each curator
action cost the vault, at the worse of spot and the 10-minute TWAP of a deep Uniswap v3 pool fixed at deployment, and
charges that measured cost in full to the curator's deferred pay and bond; an action they cannot cover reverts. When
the reference lags the market the measure misses part of the cost (a lag leak), which a delayed settlement closes in
expectation; a curator who walks away forfeits its unvested pay.

**Your curator pays for its own trades, as the pool measures them.**

**Why a curator signs up:** it gets a verifiable on-chain record of its execution conduct, a published term sheet
(Terms), and it keeps every bp it executes below cost.

- Video: to come. App: to come.
- Testnet (Unichain Sepolia, chain 1301, demo tier; the reference pool there is our own mock-token v3 pool, see
  Limitations): vault `0xA8FD240dEFEBE61200C67e8FBfbF8BFC4597b962`, hook `0x93700F94801eB5542313c87c22cd6c59e875e0A0`,
  factory `0x68b4C7D0a58f66d4f8c49B493370824a191e1fE1`, curator bundler `0xeccbecA62ffCC13f78890BA9481f7D8649f6Ae31`,
  reference pool `0x84517dBDaa924A8d9bc2abb97BAAB40A648EC972` (all in `script/demo/deployments-1301.json`, with the
  transactions of `script/demo/demoflow.sh`; deployed 2026-11-20 with the frozen 2.2% base rate).

## Four worked examples

Each example names the test or evaluation that shows it. Parameters are the mainnet tier's (see Terms). The attack tests
run on the test tier (12 s blocks, 20-block grid, base rate 1% a year) with the mainnet tier's fence, swap caps,
reserve and markout window. Amounts from them are in token units (18 decimals) of a test vault filled by depositors up to its bond cap, with
a NAV of about 1,150 units (its 0.69-unit reserve is 6e-4 of NAV).

### 1. A curator action is charged, and an uncoverable one reverts

```
 curator ──setPositions / rebalanceSwap──▶ vault
                                            │ value holdings before and after the action at three prices:
                                            │   the spot read first (s0), spot, and the reference TWAP
                                            ▼
                         charge = the worst of the three shortfalls (0 if the action gained; never netted)
                                            │
                    ┌───────────────────────┴──────────────────────────┐
     unvested pay + bond cover charge + reserve?                       no ──▶ revert Insolvent
                    │ yes                                                      (nothing moves; withdrawals stay open)
                    ▼
     burn the curator's unvested pay, then its bond   +   hold a reserve (6e-4 of NAV)
                    │
                    ▼  at the first call after the 120 s markout window (it opens one block after the action;
                    │  anyone can call settle())
     settle against the reference TWAP over that window; burn up to the reserve more if the action cost more
     (if the window can no longer be read, the whole reserve is burned)
```

- An action taken more than 60 ticks from the reference reverts `Fence` (`test_U_guard__fenceOnBothSides`).
- A gain at one price never offsets a loss at another, or a later charge
  (`test_U_net__aGainNeverOffsetsAChargeOrALaterCharge`).
- The test builds the worst legal swap (charge at least 1.37e-4 of NAV, asserted) and shows that charge plus reserve
  fits the bond twice over; with the bond at exactly the burns the swap passes, with one share less it reverts
  `Insolvent`, and depositors can still withdraw (`test_U_solv__worstSwapPlusReserveFitsTheBond`). The same holds for
  the worst legal placement (charge plus reserve at least 8.6e-4 of NAV, asserted; 8.65e-4 in the bond sizing, which
  sets the bond ratio at 1.74e-3) (`test_U_solv__worstPlacementPlusReserveFitsTheBond`).
- On simulated 180-day paths the reference curator was charged 1.6 / 4.0 / 15.7 bps of NAV a year at 40 / 60 / 120%
  annual volatility (E8).

### 2. A curator walks away

```
 reference price leaves the vault's range
        │  no base fee accrues; nothing vests (the vest gate reads the live reference)
        ▼
 observed time out of range reaches the cure window (24 h)
        │
        ▼
 breach 1 ──▶ all unvested pay is forfeited and burned to depositors over one epoch (7 days)
        │
        │   a real placement (containing spot and the reference, at least half the vault deployed),
        │   or two consecutive hourly reference samples back in range, starts a new run;
        │   a dust placement or a 1-wei swap does not
        ▼
 breach 2 ──▶ breach 3 ──▶ EXITED: the curator is removed; no replacement curator;
                           deposits close; every depositor can withdraw in kind
```

- Nothing vests while the reference is out of range (`test_U_vest_gate__liveReferenceOutBlocksTheVest`).
- Only observed time counts, and one breach is latched per cure window crossed
  (`test_U_clock__oneLatchPerCureBlocksCrossedAtTheCrossingBlock`).
- The forfeit drips: half is burned at half the drip length (`test_U_forfeit__dripBurnsHalfAtHalfTheDripLength`), so
  a deposit timed just before the burn gains only its time-weighted share (M-10, M-snipe-forfeit).
- Dust placements and 1-wei swaps cannot reset or pause the clock
  (`test_U_breach_dodge__dustAndOffSpotPlacementsCannotResetTheClock`,
  `test_U_breach_capdodge__dustSwapsCannotPauseTheClock`).
- After three breaches deposits revert `BadMode` and a full withdrawal succeeds
  (`test_U_breach__anEmptyVaultReachesExitedAndWithdrawalsStayOpen`).
- To force a breach, a third party must hold the deep reference pool's TWAP outside the range for 24 h of observed
  time. For a vault placed at its range edge, where a 10-tick push is enough, that costs about 341 WETH if arbitrage
  restores the price every block (E8: the measured one-block cost times the blocks in 24 h).

### 3. A depositor's round trip

```
 deposit ──▶ shares priced against the depositor (rounded up); refused while a settlement is pending
    │
    ▼
 curator acts ──▶ each charge burns curator-owned shares; the depositors' shares are untouched
    │
    ▼
 base fee: m (2.2% a year, see Terms) × the share of the vault deployed in range, minted to the curator as shares,
           vesting 4 to 5 epochs later through the vest gate, forfeited on a breach
    │
    ▼
 withdraw in kind, in every mode
```

- A deposit followed by a withdrawal never profits (`test_U_5__roundTripNeverProfits`).
- Deposits wait for a pending settlement (`test_U_settle__depositsBlockedWhilePendingThenSettleFirst`).
- No base fee without a curator placement, in EXITED, or at zero deployment
  (`test_U_base__noBaseWithoutAPlacementInExitedOrAtZeroDelta`).
- Withdrawals stay open after removal (`test_U_breach__anEmptyVaultReachesExitedAndWithdrawalsStayOpen`).
- Not covered: market moves, positioning and token mix; the disclosed attack tails; and two small depositor-side
  effects (a base fee minted inside a markout window slightly under-charges that settlement; a holder who leaves during
  a reference outage skips its share of the deferred base). All are listed under Limitations and in SECURITY.md.

### 4. The self-sandwich, and why it loses

```
 colluding trader pushes spot up to 60 ticks ──▶ curator places at the pushed price ──▶ trader unwinds
                                                       │
                       the reference TWAP did not move with the push, so the placement is valued
                       at the worse of the pushed spot and the reference, and the trader's gain is
                       charged to the curator in full
                                                       ▼
                                      coalition net ≤ 0 on every row
```

- The grid (three states, pushes of 20 / 30 / 60 / 100 ticks on both sides, three depths, two extreme ranges): every
  row nets ≤ 0; at least 27 rows per state act and are charged; past 60 ticks the placement reverts
  (`test_M_N4__gridLoses`, `test_M_N4__selfSandwichAroundANoSwapPlacementLoses`).
- The design model's three worst rows, replayed on v4: −0.0024 / −0.0027 / −0.0030 token units
  (`test_M_N4__pinnedPythonWorstRow1` … `Row3`).
- Positive control: with a reference that follows the push (the defence removed), the same attack nets +0.0155
  (`test_M_N4__ctlReferenceFollowingThePushProfits`).
- The same holds for JIT liquidity on the curator's own swap (`test_M_ADV3__jitOnTheCuratorsSwapLoses`; control
  +0.0273) and at an edge placement (M-N4edge). The 17 attack cells, their controls and the disclosed tails: E5 and
  SECURITY.md.

## Limitations

At every curator action, the pool prices what that action cost the vault, at the worse of spot and the 10-minute TWAP
of a deep Uniswap v3 pool for the same pair fixed at deployment, and charges that measured cost in full to the curator's
deferred pay and bond; an action they cannot cover, or one taken more than a published band from that reference,
reverts. The curator's pay never vests while the vault is outside the reference price; observed absence past a
published cure window forfeits all its unvested pay, three breaches remove it, and depositors can always exit in kind.
Market moves, positioning and token mix are not covered, skill is not rewarded, and the reference TWAP is assumed not
to be moved; the measured cost of moving it is listed under Results (E8).

**Open limits, current configuration:**
- **E5 is FAIL (control form) under its registered rule.** Every asserted attack row in all 17 cells nets ≤ 0, and 14
  cells have a separate control test that profits with the defence removed. N1 and N2 have only inline sensitivity
  checks, and the registered rule exempted N3 alone; under the rule as amended after the data E5 would pass
  (`eval/results/E5.json`).
- **Reported, not asserted, attack rows with a positive net** (E5): a markout past the settlement cap after the
  vault's own move, up to +0.93 token units (about 8e-4 of NAV); a protocol fee switched on, +0.004 (about 4e-6 of
  NAV); arbitrage stopping off the settlement's legs or overshooting in both worlds, up to +0.0006; parking the pool
  price just inside the range while the cached reference lags, up to +0.00023 per exit on the test tier (about 11
  minutes of base-fee credit at 1% a year); on the mainnet tier the asserted bound is two hours of credit per exit,
  at most m·δ·NAV over 2 h ≈ 5e-6 of NAV (M-N1 lane B). Units: 18-decimal tokens in a test vault of about 1,150
  units of NAV (its reserve of 0.69 units is 6e-4 of NAV).
- **One block builder.** The markout assumes competitive block building. A single builder that wins the action's block
  and every block of the window, censors arbitrage on the reference pool and unwinds once is outside the model; against
  it the safe NAV is about 150 WETH, against the 1,850 WETH cap.
- **The NAV cap is checked on the NAV priced at the last action**, so a vault left unplaced through a large move can
  hold more than the cap (about +25% after a 50% move with half the NAV in token0).
- **E-IC (incentives on the historical replay) was not run.** No historical replay exists. U-IC2 covers E-IC's
  comparison clause on synthetic paths without an outside observer or third-party liquidity; it does not cover the
  per-tier term sheet, the curator's 5th-percentile condition or the depositor ceiling. A supplement (Results) runs one
  historical price series through the same simulator; it gives point estimates on one path and no verdict.
- **The allocation ledger carries no closure claim** (E0b failed; below).
- **Not audited.** The contracts have not been audited; the tests, evaluations and SECURITY.md are the evidence.
- **Testnet.** Unichain Sepolia has no usable v3 reference pool, so the demo deploys its own mock-token pool; the
  security evidence for the reference is the mainnet-fork test K-ref.

**Lag leak and its repair (measured).** Every price an action can read is at least one block behind the market, so
a charge at the worse of spot and the TWAP alone leaves a lag leak: a curator colluding with a trader can place
liquidity at a stale price and let the trader take the difference (on synthetic paths 8.3% / 37.2% of NAV a year at
60% / 120% annual volatility with the price push free to the fence). Each action is therefore also settled against
the reference pool's TWAP over a short window that opens one block after the action, with a fixed reserve held until
settlement; the curator commits before that price exists. On held-out paths the leak measured 0.000% of NAV a year at
both volatilities (a model cell on the shipped settlement, run before the 12-hour reference policy was adopted; E8
copies the value; its source record is not public). The guarantee is in expectation: the reserve caps what one settlement can recover, so a colluding
pair can still profit on an extreme markout draw (beyond about 150 ticks for a 1.9%-of-NAV action), and an arbitrage
that stops outside the settlement's fee band is not covered by its legs.

**Curator incentives (measured without a third-party refresher of the reference; pre-registered test U-IC2,
passed).** With the evaluated base rate m = 2.2% a year and no third-party refresher of the reference, the reference curator, which re-places its range once the reference price has been outside it for 12 hours (edge-12h),
out-earns every gated strategy on 400 fresh simulated 180-day paths per bucket, run once with its form, seeds and pass
rule frozen and committed before it started. The strategies are seven lazy variants that never re-place (minimum lower
bound of the difference +1.00 ETH), a placement of the smallest legal deployment just before the cure window, the
cheapest legal clock reset, and idle, dust, stale and wide curators. The binding case is 120% annual volatility:
edge-12h minus the clock reset is +0.235 ETH (one-sided 95% lower bound +0.166 ETH) and minus the smallest-deployment
placement +0.275 ETH (lower bound +0.205 ETH); at 40%, 60% and on a path that hovers at the range edge every lower bound
exceeds 1.4 ETH. The power controls pass in the three volatility buckets (with no base fee, the lazy curator beats the
reference curator) and not on the hovering path (−0.026 ETH; reported, not gated). On the same paths the 12-hour wait out-earns the earlier 6-hour wait at 120% volatility by +0.187
ETH (lower bound +0.163 ETH), the gated check that the result comes from the change. Results:
`eval/results/U-IC2.json`, `eval/supplements/U-IC2-observer.json` (the observer-environment comparisons for every
strategy were computed after the run from the same per-run rows).

**Limits of this result.** These are simulations on synthetic paths with no third-party liquidity, not the historical
replay. The gated comparisons assume that no outside party refreshes the reference price. With an outside observer
refreshing it every two hours (reported, not gated), the cheapest clock reset and the smallest-deployment placement
out-earn the reference curator at 120% volatility (by 0.151 and 0.102 ETH), the clock reset roughly ties it at 40%
and edges it on the hovering path. Incentive compatibility here refers to the curator's net pay only: in this model,
the vault ends with more value under the two lax strategies than under the reference curator in every volatility
bucket (at 120%: about 558 and 569 against 314, from 1,000; not on the hovering path), because time in range at zero third-party depth is mostly loss to
arbitrage. The 12-hour wait is a trade made for high volatility: on the same paths it earns 0.11 to 0.13 ETH
less than the 6-hour wait at 40% and 60% volatility and on the hovering path (U-IC2 lever rows; it still passes there), and at 120%
volatility the vault is in range 76% of the time instead of 82%. The wait was chosen after the earlier test failed,
from the three waits simulated since the start (3, 6 and 12 hours; edge-12h was a reported column of that test), as the
only one passing on all three earlier seed sets; waits between 13 and 20 hours were not simulated, and a 24-hour wait
is not deployable with the 24-hour cure window. The contract code is unchanged; the wait is a deployment constant.

**Earlier configuration (measured, two negative results).** With a 6-hour wait (edge-6h), the same rule failed twice
and both results stay published. On held-out paths, at m = 2.2% a year and 120% annual volatility, edge-6h was not
shown to out-earn two lax strategies over 180 days: a placement of the smallest legal deployment at or above the
minimum just before the cure window (edge-6h minus it: mean +0.036 ETH, confidence lower bound −0.052 ETH) and the
cheapest legal clock reset (mean −0.016 ETH, lower bound −0.102 ETH). All other gated rows passed. The smallest rate
that would close the gap was estimated at no less than about 2.4% a year and was not measured; m was not changed after
seeing the data.

The same rule was then run once more, on 200 fresh simulated paths per bucket (test U-IC), with its form, seeds and
pass rule frozen and committed before it started. The same two comparisons failed: at 120% volatility, edge-6h minus
the strategy that places the smallest legal deployment at or above the minimum just before the cure window was +0.086
ETH (lower bound −0.011 ETH), and minus the clock reset +0.012 ETH (lower bound −0.084 ETH). The other 50 comparisons
passed, as did the conditions on the reference curator's own net; the power control passed in the three volatility
buckets and not on the hovering path (−0.020 ETH; reported, not gated). With an outside observer refreshing the
reference every two hours (reported, not gated), both strategies out-earned edge-6h at 120% volatility (by 0.32 and
0.39 ETH). The smallest passing rate was estimated at 2.38% a year without an observer and about 8.1% with one, both
extrapolated beyond the simulated rates. U-IC stays failed and is never re-run; U-IC2 is a separate test of the
changed wait, not a replacement. On U-IC2's paths edge-6h fails against the clock reset (+0.049 ETH, lower bound −0.022
ETH); against the smallest-deployment placement it passes there (+0.088 ETH, lower bound +0.017 ETH). Results: `eval/results/U-IC.json`, `eval/supplements/U-IC-observer.json`.

**Allocation ledger (disclosure only; no closure guarantee).** The allocation ledger is published for disclosure and
never affects pay. Its pre-registered test asks each epoch's allocation to reconcile the vault's value change for a fixed
depositor to within 0.3 bp. In the current configuration that test, E0b, failed, so the ledger carries no closure claim:
- E0b reads the vault's own fee leg from pool state at each vault call (no contract change) and was run once on eight
  pre-registered scripts (seeds 1100–1107) built to stress the remaining approximation, the passive leg's time-based fee
  rate. Every script completed, the share terms reconciled exactly, every non-vacuity condition held and every gated
  control had been shown powered before registration, but 59 of 112 epoch rows exceeded the bound (worst 4.09 bp; per
  script 1.27–4.09 bp), mostly in pairs of neighbouring epochs with opposite signs. A diagnosis made after the run
  attributes the residual to the passive leg (its time-based rate and its valuation): with the passive leg's fees
  replaced by their true values from pool state, every row closes within 0.022 bp (`eval/supplements/e0b_diagnosis.py`).
  The ledger attributes a witness lifetime's fees in proportion to credited time, not to when they were earned; on these
  scripts its per-epoch figures were off by up to 4.09 bp, and no bound is claimed outside them. The runs use the test
  tier with the base rate raised, as in E0a. No third attempt is made; reading the witness's fee growth at each call is
  future work and would not count toward E0, E0a or E0b.
- The test was registered before any held-out script existed (commit `392784e`, branch `e0b`); its frozen form,
  generator, harness and evaluator are in `eval/E0b-form.md`, `eval/e0b_gen.py`, `test/eval/E0bData.t.sol` and
  `eval/E0b.py`; reproducing the verdict needs the branch `e0b` (the per-seed manifests committed after registration).
  Results: `eval/results/E0b.json`, `eval/results/E0b-fit.json`, `eval/supplements/E0b-H.json`.

Earlier configuration (published as failed, final):
- E0, the original formula: worst residual 58.6 bp in an epoch for a curator offset from the benchmark (reduced
  form); on the E0a runs the same formula misses by up to 0.99, 38.6 and 147 bp.
- E0a, the ledger amended once after E0's data: worst residual 0.0074 bp (`same`) and 0.0058 bp (`offset`), but 0.556
  bp in one epoch of the `exit` run. The following diagnosis was made after the run, from that one epoch, whose exit
  was a single scripted in-block swap of about 400 ticks that took the price out of the vault's range, right after a
  poke on a stale reference cache: the ledger credits fees only to blocks it observes in range under a fresh reference
  cache, so in this epoch it booked the vault's fees as zero (the residual equals the fees collected, to 2e-11 bp; the
  stale span and the crossing are not separated). The form registered before the run (frozen before the harness commit
  `72e946e`) read: "fees of unobserved in-range time, which are spread over the observed exposure", and listed "fee
  non-uniformity within a lifetime" and "the τ_v reference gate" as error terms measured by the residual; in this
  epoch such fees were not spread but dropped, and the residual exceeded the bound. One of the ten controls
  (`fee_by_n`, 0.2975 bp) also falls short of the bound, so it lacks the power to detect its fault. The evidence is
  three scripted runs on the test tier with the base rate raised about 3,150 times (to make the base terms
  measurable); every share row closes exactly.

Results: `eval/results/E0.json`, `eval/results/E0a.json`.

## How it works

```
                 deposit / withdraw (in kind)              curator (through CuratorBundler)
 depositors ───────────────────────────────▶  TouchstoneVault  ◀──── setPositions, rebalanceSwap,
                                              (+ Extension)          claim, topUpBond, retire
                                                    │ one position, width W
                                                    ▼
 any trader ── swap ──▶  Uniswap v4 pool  ──callbacks──▶  TouchstoneHook ──observe──▶  Uniswap v3 reference pool
                                                    (clock, reference cache,           (10-min TWAP; 120 s markout)
                                                     witness position, records)
```

- **Contracts.** `TouchstoneFactory` deploys a vault, its extension and its hook together and seeds the vault.
  `TouchstoneVault` holds the tokens, one liquidity position and the curator's bank and bond, and runs every action;
  `TouchstoneVaultExtension` holds its less frequent entry points (seed, claim, bond, retire, poke, breach processing).
  `TouchstoneHook` keeps the breach clock, the cached reference price, a small witness position and the records.
  `CuratorBundler` lets a curator deploy and place in one transaction and re-align spot inside its own transaction.
- **The charge.** Each curator action values the vault's holdings before and after at the pre-action spot, the spot
  and the live reference TWAP, and charges the worst shortfall, floored at zero per action and never netted. It is paid
  by burning the curator's shares: deferred pay first, then the bond. A fixed reserve (6e-4 of NAV) is held; the next
  call after the 120 s markout window settles the action against the reference TWAP over that window (fee-aware legs)
  and burns up to the reserve more. If bank plus bond cannot cover charge plus reserve, the action reverts.
- **Guards.** Width exactly W; the range must contain spot and the reference; spot within 60 ticks of the reference
  (the fence); per-action and per-epoch swap caps (2% and 5% of NAV); a cooldown; a deployed share δ of at least 0.5
  for a placement; an optional `maxCharge` set by the curator.
- **The base fee.** Accrues at m × δ per unit of observed time in which the pool price is strictly inside the range
  and the cached reference is in range. It is minted to the curator as shares, held for 4 epochs, and vests only at a
  checkpoint where the live reference is in range and no unobserved gap is open.
- **The breach ladder.** Observed time with the cached reference outside the range counts toward the 24 h cure window.
  Each window crossed is a breach: all unvested pay is forfeited and dripped to holders over one epoch. A real
  placement starts a new run. The third breach moves the vault to EXITED (curator removed, deposits closed,
  withdrawals open). No one can re-place the range for the curator.
- **No keeper, operator, admin or off-chain feed.** The one external input is the reference pool's TWAP, read through
  its oracle's `observe`, never its `slot0` price. Anyone can call `poke`, `processBreach`, `settle`, `refreshRef` and
  `snapshotIfNewBlock`. The records' resolution equals the poke cadence.

## Terms

Mainnet tier (USDC/WETH 0.05% reference pool `0x88e6A0c2dDD26FEEb64F039a2c41296FcB3f5640`), from `script/Tiers.sol`:

| Term | Value |
|---|---|
| Base fee m | 2.2% a year × δ (the deployed share), in range only; the evaluated rate, frozen in `script/Tiers.sol` for the mainnet and demo tiers (the forge test tier runs 1% a year) |
| Range width W | 2,000 ticks (about ±10%) |
| Fence around the reference | 60 ticks |
| Swap caps | 2% of NAV per action, 5% per epoch, 20 swaps per epoch |
| Bond | 1.74e-3 of NAV (`BOND_RATIO`), at least 1.74 WETH |
| Reserve per action | 6e-4 of NAV, held for a 120 s markout window |
| NAV cap | 1,850 WETH |
| Cure window | 24 h of observed time out of range (7,200 blocks) |
| Epoch | 7 days (50,400 blocks); pay vests after 4 epochs |
| Breaches to removal | 3 |
| Reference curator policy | re-place 12 h after the reference leaves the range (edge-12h) |

**Why would a curator sign up?** It gets a verifiable on-chain record of its execution conduct, which depositors can
check; it is paid a published base fee for capital it keeps deployed; and it keeps every bp it executes below cost.

## Results

Each row is a pre-registered evaluation with its result file in `eval/results/`. "Synthetic" means simulated 180-day
price paths (driftless GBM at 40/60/120% annual volatility and a path hovering at the range edge), with no third-party
liquidity and gas at 5 gwei. No row uses a historical replay. The per-path simulation rows behind E4 and E8 (670 MB)
are not in the repository; their hashes and aggregates are (`eval/supplements/U-IC2-agg.json`).

| ID | Question | Result | Data |
|---|---|---|---|
| E1 | Is the witness a real position? | PASS: 0 wei difference, exact equality on 256 fuzz runs | Foundry |
| E2 | Does the charge cover the realised loss of an honest placement? | PASS: the expected charge (over 400 draws of the reference window's error) covers the realised loss on all 2,016 / 2,100 sampled exit states (σ 60 / 120%, the shipped settlement); overcharge 0.012 / 0.023 bp of NAV per placement (mean), p99 0.13 / 0.34 bp. A simpler settlement rule, not shipped, falls short on 16 / 21 | Synthetic (GBM), single-placement cell |
| E3 | Is the charge zero without a curator action? | PASS: 0 wei over 10,000 segments and 20,034 third-party swaps; control: one swap is charged | Independent model |
| E4 | Does the reference curator out-earn idle, dust, stale and wide curators? | PASS in every bucket; idle nets exactly 0 before gas and loses its pokes' gas (−0.136 ETH) | Synthetic (U-IC2 runs) |
| E5 | Do the asserted attacks lose money? | FAIL (control form) under the registered rule: every asserted attack row in all 17 cells nets ≤ 0; 14 cells have a control test that profits with the defence removed; N1 and N2 have only inline sensitivity checks (the rule exempted N3, a forgery cell, alone). Under the rule as amended after the data: PASS. Reported, not asserted, positive rows: +0.93 (capped markout), +0.004 (protocol fee), +0.0006 (off-leg stops), up to +0.00023 per exit on the test tier (M-N1 parking; ≤ 5e-6 of NAV per exit on the mainnet tier), in token units of a vault of about 1,150 | Foundry |
| E6 | Does churning pay? | PASS: churner minus reference curator, t = −129 to −691 per bucket (follows from construction) | Synthetic |
| E7 | Gas and size within budget? | PASS: largest runtime 19,877 B (limit 22,000); first swap per block 26.7k gas (budget 30k); worst action 871k (1.2M) | Foundry and mainnet fork |
| E8 | Operating numbers | REPORTED: 11 items (one closed form, two from earlier configurations); 6 not run: suspect-segment share, p99 actions per epoch, curator recapture of burns, time to feasibility with one-sided inventory, gate-closed share, depositor observer break-even | Mixed, labelled per item |
| E9 | Why is positioning not paid? | REPORTED: a carry benchmark pays a random curator +40 ± 10% / +6 ± 6% / −13 ± 6% above base across three regimes; an exposure-matched one +0.1 ± 1.7% / +0.1 ± 1.7% / −0.9 ± 1.5% | Design-phase simulator |
| E-IC | Incentive compatibility on the historical replay | NOT RUN: the replay was never fetched; U-IC2 covers its comparison clause on synthetic paths (no outside observer, no third-party liquidity), not the term sheet, the curator's 5th-percentile condition or the depositor ceiling | — |
| E0b | Does the allocation ledger close within 0.3 bp per epoch? | FAIL (59 of 112 epoch rows above the bound, worst 4.09 bp); the closure claim is withdrawn (Limitations) | Foundry, 8 pre-registered scripts |

E-IC historical supplement (one historical price series through the synthetic simulator, no third-party depth, point estimates, not a replay and not E-IC's rule): blocks 25,147,070 to 26,443,420; edge-12h net +11.282 / +1.123 ETH at NAV 1,850 / 185; gated bots edge-12h does not beat: none; E-IC stays NOT RUN. Unreviewed. Form, data and attempts: `eval/E-IC-supplement-form.md`, `eval/supplements/E-IC-historical.json`, `eval/supplements/E-IC-hist/`.

**Rebalancing cost charged to the reference curator** (E8; synthetic): 1.6 / 4.0 / 15.7 bps of NAV a year at 40 / 60 /
120% volatility, 0.8 bps on the hovering path.

**Cost of moving the reference TWAP** (E8; measured on a mainnet fork at block 26443420, the USDC/WETH 0.05% pool):

| Push | Cost to hold one block | Round trip | Hold for the 24 h cure window (arithmetic) |
|---|---|---|---|
| 10 ticks | 0.047 WETH | 0.048 WETH | 341 WETH |
| 30 ticks | 0.294 WETH | 0.148 WETH | 2,113 WETH |
| 60 ticks | 1.04 WETH | 0.298 WETH | 7,466 WETH |
| 120 ticks | 3.85 WETH | 0.596 WETH | 27,714 WETH |

The 24-hour figures assume arbitrage restores the price every block. A single builder that wins every block and censors
arbitrage pays about one round trip instead; that case is outside the model.

## Claims → tests

Every claim ID in the specification has a test named `test_<ID>__<claim>` (or `invariant_<ID>__…`) or a result file
`eval/results/<ID>.json`; `test/CLAIMS.md` lists them by phase and `script/claims.py` fails the build on a missing one.

- `CLAIMS_PHASE=P6 python3 script/claims.py` prints: "104/106 current claim IDs due by P6 covered + 3 historical
  failed (E0, E0a, U-IC; earlier configuration, final, never met) + 1 withdrawn (E0b; failed, claim withdrawn, never
  met)", with E5 (FAIL, control form) and E-IC (not run) missing.
- Curated mutation testing: 332 mutants listed, 328 wired, 4 equivalent (outside the denominator), every wired mutant
  killed by the test it names (`audits/mutants.json`, `python3 script/mutate.py --audit`).
- Invariants run at 64 × 64 in `forge test` and at 256 × 128 in `script/ci-local.sh`, followed by a non-vacuity check
  that the campaign executed swaps, placements, moves, settlements and vested pay (`script/nonvacuity.py`).
- An independent Python reference model (`model/reference.py`), written from the specification, produces golden
  vectors that the contracts match bit for bit (`test/vectors/`).

## Security and attacks

SECURITY.md lists every threat with its mitigation and its regression test, and the open cells. In short: 17
attack cells assert that the attack loses money (E5); the reported, not asserted, rows with a positive net are listed
under Limitations and in SECURITY.md. Forging the disclosure ledger by price oscillation costs the coalition about 7.8
token units per moved boundary median in the test vault (about 7e-3 of NAV) (`test_M_N3__oscillationCannotForgeTheLedgerAndLoses`).
REVIEWERS.md gives five claims to check in a few minutes.

## Run

```
forge build --sizes src                                   # every runtime ≤ 22,000 B
export FORK_BLOCK_1=26443420 MAINNET_RPC_URL=<archive RPC> # fork tests pin this block; unset, they return early (PASS)
forge test                                                # unit, fuzz, manipulation, invariant (64 × 64), fork
CLAIMS_PHASE=P6 script/ci-local.sh                        # the CI steps, 256 × 128 invariants, non-vacuity
python3 model/reference.py --selfcheck                    # the independent reference model
python3 script/mutate.py --audit                          # the curated mutant list
python3 eval/e5.py --forge-log eval/supplements/E5/forge-manipulation-s27.log  # re-derive E5 from its saved log
```

## Uniswap v4 pitfalls we hit

- A hook's own calls skip its own callbacks, so the vault is a separate contract and calls
  `hook.snapshotIfNewBlock()` before an action without a swap.
- `modifyLiquidity` pays the caller every fee the position has accrued; every entry point collects first.
- A zero-delta modify reverts on an empty position.
- Ticks clear when their gross liquidity reaches zero and lose their fee-growth history; the witness is moved by
  reading the old one, adding the new one, then removing the old one, with a fresh salt per epoch.
- `getFeeGrowthInside` is only meaningful as a delta while both ticks stay initialized, with an unchecked subtraction.
- An `unlock` inside another `unlock` reverts; the vault's entry points are not composable inside one.
- In hook callbacks `msg.sender` is the router, not the user.
- `forge test` deploys contracts above the EIP-170 size limit without complaint; the size gate is a separate step.

## Rejected designs

- **Reimbursing measured skill** (refunding part of the charge when the curator beats a benchmark) lowers the
  effective charge, so self-dealing is refunded too: JIT self-dealing earns about 10× the base fee under a charge
  scaled by such a factor (E8, rejected-design row), against ≤ 0 under the full charge (E5, M-ADV3).
- **Paying against a carry benchmark** pays a zero-skill random curator +40 ± 10%, +6 ± 6% and −13 ± 6% above base in
  three market regimes; an exposure-matched benchmark removes the regime effect but then pays nothing for positioning
  (E9). So positioning is not paid.
- **A variance-scaled base fee** adds a term that depends on the price path between actions, which can be pumped.
- **A standard-cost allowance** (a fixed budget per action) is farmed by a curator that hugs the range edge and acts
  as often as the allowance pays.
- **Zero-fee vault swaps** save little on edge placements, need an irreversible dynamic-fee pool key, and make
  third-party liquidity providers subsidise the vault.

## Prior art and credits

Implementation shortfall (Perold, 1988); deferral and malus of variable pay; loss-versus-rebalancing (Milionis et
al.); passive-LP benchmarks. Dependencies and reused code: CREDITS.md.

Partner integrations: None.
