# Credits

- Uniswap v4 core and periphery, OpenZeppelin uniswap-hooks and contracts, forge-std — dependencies (see `lib/`).
- Prior art the design builds on: implementation shortfall (Perold, 1988); deferral and malus of variable remuneration
  (EU CRD IV/V); Brinson-style performance attribution; loss-versus-rebalancing (Milionis et al.); passive-LP benchmarks
  (Sadeghi, Liu, Moallemi, Wan, Zhu, 2026, arXiv 2609.37963).
- The mutation-testing runner pattern (on-disk single mutation, named killer test, in-progress marker) is adapted from an
  earlier Foundry project's harness.
- The reference model's integer math (`model/v4math.py`) ports `FullMath`, `TickMath`, `SqrtPriceMath` and `SwapMath`
  from Uniswap v4-core and `LiquidityAmounts` from Uniswap v4-periphery (MIT), keeping their rounding.
- The reference model (`model/reference.py`) started from an internal spike of this project; its valuation was kept
  where it matched the current design and its pay rules were rewritten.
