# Touchstone

A Uniswap v4 hook vault in which the pool keeps the curator's books.

## Summary

Today, when a manager re-positions a liquidity vault, the trading costs of that move (swap fees, price impact, value
lost to front-running or to a friendly counterparty) come out of depositors' money, and the manager is paid regardless.
Touchstone reverses that: the pool itself measures what each manager action cost depositors and charges that measured
cost in full to the manager's held-back pay and a bond they post; an action they cannot cover is refused. The
measurement uses on-chain prices; when the reference price lags the market it would miss part of the cost (a lag
leak), which a delayed settlement closes in expectation (measured and published below). The manager earns a base fee
only while the vault's capital is deployed inside its agreed price range, so keeping the vault in range is how the
manager gets paid; pay never vests while the vault is out of range, and an observed absence past a published cure window
forfeits the held-back pay.

**Your curator pays for its own trades, as the pool measures them.**

## What is and is not covered

At every curator action, the pool prices what that action cost the vault, at the worse of spot and the 10-minute TWAP
of a deep Uniswap v3 pool for the same pair fixed at deployment, and charges that measured cost in full to the curator's
deferred pay and bond; an action they cannot cover, or one taken more than a published band from that reference, reverts. The curator's
pay never vests while the vault is outside the reference price; observed absence past a published cure window forfeits all
its unvested pay, three breaches remove it, and depositors can always exit in kind. Market moves, positioning and token mix are not covered, skill is not
rewarded, and the reference TWAP is assumed not to be moved; the measured cost of moving it will be published with
the security results.

**Lag leak and its repair (measured).** Every price an action can read is at least one block behind the market, so
a charge at the worse of spot and the TWAP alone leaves a lag leak: a curator colluding with a trader can place
liquidity at a stale price and let the trader take the difference (on synthetic paths 8.3% / 37.2% of NAV a year at
60% / 120% annual volatility with the price push free to the fence). Each action is therefore also settled against
the reference pool's TWAP over a short window that opens one block after the action, with a fixed reserve held until
settlement; the curator commits before that price exists. On held-out paths the leak measured 0.000% of NAV a year at
both volatilities. The guarantee is in expectation: the reserve caps what one settlement can recover, so a colluding
pair can still profit on an extreme markout draw (beyond about 150 ticks for a 1.9%-of-NAV action), and an arbitrage
that stops outside the settlement's fee band is not covered by its legs.

**Curator incentives (measured without a third-party refresher of the reference; pre-registered test U-IC2,
passed).** With the shipped base rate m = 2.2% a year and no third-party refresher of the reference, the reference curator, which re-places its range once the reference price has been outside it for 12 hours (edge-12h),
out-earns every gated strategy on 400 fresh simulated 180-day paths per bucket, run once with its form, seeds and pass
rule written and reviewed before it started. The strategies are seven lazy variants that never re-place (minimum lower
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
arbitrage. The 12-hour wait is a trade made for high volatility: on the earlier test's paths it earns 0.09 to 0.13 ETH
less than the 6-hour wait at 40% and 60% volatility and on the hovering path (it still passes there), and at 120%
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
pass rule written and reviewed before it started. The same two comparisons failed: at 120% volatility, edge-6h minus
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
  rate. Every script completed, the share terms reconciled exactly and every control and non-vacuity condition held, but
  59 of 112 epoch rows exceeded the bound (worst 4.09 bp; per script 1.27–4.09 bp), mostly in pairs of
  neighbouring epochs with opposite signs. A diagnosis made after the run attributes the whole residual to that fee rate: with the passive leg's
  fees replaced by their true values from pool state, every row closes within 0.022 bp. The ledger attributes a witness
  lifetime's fees in proportion to credited time, not to when they were earned; its per-epoch figures can therefore be
  off by a few basis points under bursty trading and late pokes. The runs use the test tier with the base rate raised,
  as in E0a.
- The test was registered before any held-out script existed (commit `392784e`, branch `e0b`); its frozen form,
  generator, harness and evaluator are in `eval/E0b-form.md`, `eval/e0b_gen.py`, `test/eval/E0bData.t.sol` and
  `eval/E0b.py`. Results: `eval/results/E0b.json`, `eval/results/E0b-fit.json`, `eval/supplements/E0b-H.json`.

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

There is no keeper, operator, admin or off-chain feed. The one external input is the reference pool's TWAP, because a
new hook pool whose vault is out of range has no reliable price of its own.

> Status: under active development. Results, run commands, the claims → tests table and limitations will be listed here.

Partner integrations: None.
