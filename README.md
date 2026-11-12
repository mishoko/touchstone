# Touchstone

A Uniswap v4 hook vault in which the pool keeps the curator's books.

At every curator action, the hook prices that action's implementation shortfall — at the worse of spot and the hook's own
block-start median price — and charges 100% of it to the curator's deferred pay and first-loss bond; actions the bond
cannot cover revert. Between actions the charge is exactly zero. The base fee accrues in proportion to the mandate
liquidity deployed in range, and an indicative allocation ledger against the passive mandate is published, not paid.

> Status: under active development. Results, run commands, the claims → tests table and limitations will be listed here.

Partner integrations: None.
