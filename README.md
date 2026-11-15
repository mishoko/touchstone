# Touchstone

A Uniswap v4 hook vault in which the pool keeps the curator's books.

## Summary

Today, when a manager re-positions a liquidity vault, the trading costs of that move (swap fees, price impact, value
lost to front-running or to a friendly counterparty) come out of depositors' money, and the manager is paid regardless.
Touchstone reverses that: the pool itself measures what each manager action cost depositors and charges that measured
cost in full to the manager's held-back pay and a bond they post; an action they cannot cover is refused. The
measurement uses on-chain prices; when the reference price lags the market it misses part of the cost (a lag leak,
measured and published below). The manager earns a base fee
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

**Known limitation (measured).** The charge prices an action at the worse of spot and a 10-minute TWAP. When the market
moves beyond both within the TWAP's lag, a curator colluding with a trader can place liquidity at a stale price and let
the trader take the difference; honest placements made while the TWAP lags are also mis-charged (some of their loss to
arbitrage goes uncharged, others are overcharged). On synthetic paths this lag leak measured 1.2% of NAV a year at 60%
annual volatility (90% CI 0.6–1.8%) on a lane that fixes the size of the price push, and more when the push is free.
Widening the charge to the reference pool's current price ± a margin closes it only with a margin wide enough to
overcharge honest curators heavily. This is an open problem of the design and is reported as a negative result.

There is no keeper, operator, admin or off-chain feed. The one external input is the reference pool's TWAP, because a
new hook pool whose vault is out of range has no reliable price of its own.

> Status: under active development. Results, run commands, the claims → tests table and limitations will be listed here.

Partner integrations: None.
