# Credits

- Uniswap v4 core and periphery, OpenZeppelin uniswap-hooks and contracts, forge-std — dependencies (see `lib/`).
- Prior art the design builds on: implementation shortfall (Perold, 1988); deferral and malus of variable remuneration
  (EU CRD IV/V); Brinson-style performance attribution; loss-versus-rebalancing (Milionis et al.); passive-LP benchmarks
  (Sadeghi, Liu, Moallemi, Wan, Zhu, 2026, arXiv 2609.37963).
- The mutation-testing runner pattern (on-disk single mutation, named killer test, in-progress marker) is adapted from an
  earlier Foundry project's harness.
