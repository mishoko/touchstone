# Touchstone

A Uniswap v4 hook vault in which the pool keeps the curator's books.

## Summary

Today, when a manager re-positions a liquidity vault, the trading costs of that move (swap fees, price impact, value
lost to front-running or to a friendly counterparty) come out of depositors' money, and the manager is paid regardless.
Touchstone reverses that: the pool itself measures what each manager action cost depositors and charges 100% of it to
the manager's held-back pay and a bond they post; an action they cannot cover is refused. The manager earns a base fee
only while the vault's capital is deployed inside its agreed price range, so keeping the vault in range is how the
manager gets paid, and leaving it out of range past a published cure window forfeits the held-back pay.

**Your curator pays for its own trades.**

## What is and is not covered

At every curator action, the pool prices what that action cost the vault, at the worse of spot and its own
block-start median, and charges 100% of it to the curator's deferred pay and bond; an action they cannot cover reverts.
Market moves, positioning and token mix are not covered, skill is not rewarded, and the valuation assumes the pool's
median is not moved. The measured cost of moving it will be published with the security results.

> Status: under active development. Results, run commands, the claims → tests table and limitations will be listed here.

Partner integrations: None.
