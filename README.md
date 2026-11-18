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

**Known limitation (measured, negative result).** At the shipped base rate m = 2.2% a year and 120% annual volatility,
the reference curator (edge-6h) is not shown to out-earn two lax strategies on held-out paths over 180 days: a
placement at exactly the minimum deployment just before the cure window (edge-6h minus it: mean +0.036 ETH, confidence
lower bound −0.052 ETH) and the cheapest legal clock reset (mean −0.016 ETH, lower bound −0.102 ETH). All other gated rows pass. The smallest rate that would close the gap is estimated at no less than about 2.4% a year on the held-out paths and has not
been measured; m was not changed after seeing the data.

**Allocation ledger (measured, two negative results).** The allocation ledger is published for disclosure only and
never affects pay. Its pre-registered test requires each epoch's allocation to reconcile the vault's value change for a
fixed depositor to within 0.3 bp. Both registered forms fail, and both are published as failed:
- E0, the original formula: worst residual 58.6 bp in an epoch for a curator offset from the benchmark (reduced
  form); on the E0a runs the same formula misses by up to 0.99, 38.6 and 147 bp.
- E0a, the ledger amended once after E0's data: worst residual 0.0074 bp (`same`) and 0.0058 bp (`offset`), but
  0.556 bp in one epoch of the `exit` run. The following diagnosis was made after the run, from that one epoch, whose
  exit was a single scripted in-block swap of about 400 ticks out of the vault's range right after a poke on a stale
  reference cache: the ledger credits fees only to in-range blocks seen at a snapshot, so in this epoch it booked the
  vault's fees from that swap as zero (the residual equals the fees collected, to 2e-11 bp). The form registered
  before the run read: "fees of unobserved in-range time, which are spread over the observed exposure", and listed
  "fee non-uniformity within a lifetime" and "the τ_v reference gate" as error terms measured by the residual; in this
  epoch they were not spread but dropped. One of the ten controls (`fee_by_n`, 0.2975 bp) also falls short of the
  bound, so it lacks the power to detect its fault. The evidence is three scripted runs on the test tier with the base
  rate raised about 3,150 times (to make the base terms measurable); every share row closes exactly.

No further amendment will be made for this release; a fee leg based on fee growth is left as future work.
Results: `eval/results/E0.json`, `eval/results/E0a.json`.

There is no keeper, operator, admin or off-chain feed. The one external input is the reference pool's TWAP, because a
new hook pool whose vault is out of range has no reliable price of its own.

> Status: under active development. Results, run commands, the claims → tests table and limitations will be listed here.

Partner integrations: None.
