# Touchstone

**Your manager pays for their own bad trades.**

A Uniswap v4 hook vault that checks every manager action against a deep market's price, and charges any shortfall to
the manager's held-back pay.

You hand your money to a vault manager (the curator) you cannot watch, and you pay them anyway. Every move the curator
makes can carry a cost: swap fees, a bad price, or a friendly counterparty who profits. Touchstone bills the cost it
measures to the curator, not to you. Market moves stay with depositors. The full paragraph: [docs/DETAILS.md](docs/DETAILS.md#the-idea-in-full).

**Why a curator signs up:** it gets a verifiable on-chain record of its execution conduct, a published term sheet
([Terms](docs/DETAILS.md#terms)), and it keeps every bp it executes below cost.

- Live demo: https://touchstone.nexus/ · Video walkthrough: https://youtu.be/mxlrBo-dVpo
- Live on the Unichain Sepolia testnet: addresses under [Contracts](#contracts).

## Twenty seconds

```
 manager acts ──▶ is spot within 60 ticks of the reference? ── no ──▶ refused, nothing moves
                        │ yes
                        ▼
        value the vault before and after, at the first spot, spot and the reference
                        │
                        ▼
        the bill = the worst shortfall ──▶ held-back pay ──▶ bond ──▶ can't cover it? refused
                        │      the curator's burned shares raise every other share
                        │
                        ▼  120 s later, anyone can call settle()
        a second check against the reference over those 120 s; up to a small reserve more
```

No keeper, no admin, no off-chain feed: the reference is the 10-minute TWAP of a deep Uniswap v3 pool, fixed at
deployment, and every check (settle, poke, breach processing) can be run by anyone.

## Five plays

The same five scenes run interactively on the [live demo](https://touchstone.nexus/#scenes); each names the test that proves it.

1. **A fair rebalance: billed.** The measured cost comes out of the curator's held-back pay, then its bond; a gain
   never offsets it (`test_U_net__aGainNeverOffsetsAChargeOrALaterCharge`). On testnet, a `rebalanceSwap` was billed
   on chain, most of it from the bond (`script/demo/deployments-1301.json`, A3).
2. **A pushed price: refused.** An action more than 60 ticks from the reference reverts `Fence` and moves nothing
   (`test_U_guard__fenceOnBothSides`; on testnet, spot pushed 90 ticks: A2 reverted `Fence()`).
3. **The manager walks away: fired.** Vesting stops while the reference is out of range; 24 hours of observed time out
   of range is a breach that forfeits all unvested pay to the holders over one epoch; three breaches remove the
   curator; withdrawals stay open (`test_U_vest_gate__liveReferenceOutBlocksTheVest`,
   `test_U_breach__anEmptyVaultReachesExitedAndWithdrawalsStayOpen`).
4. **A self-sandwich: it loses.** Worked below.
5. **Your round trip: it never profits, and you can always leave.** Shares are priced against the depositor, deposits
   wait for a pending settlement, withdrawals are in kind in every mode (`test_U_5__roundTripNeverProfits`,
   `test_U_settle__depositsBlockedWhilePendingThenSettleFirst`).

### Play 4, worked: the self-sandwich, and why it loses

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

The other three worked examples (a charge and an uncoverable revert, a walk-away, a round trip), each with its flow
and tests: [docs/DETAILS.md](docs/DETAILS.md#four-worked-examples).

## The deal

The curator earns 2.2% a year only on money placed in range, held back at least four to five weeks, and risks a bond of 0.174%
of the vault. In simulation, without a third-party refresher of the reference, the reference curator takes home
5.65 ETH over 180 days on a 1,000 ETH vault at 60% volatility; the closest gated alternative (the
cheapest clock reset) 4.17; a curator that never re-places, 0.94 to 2.23 (U-IC2, synthetic; the 12-hour re-placement wait was chosen after an earlier test
failed, see [DETAILS, Limitations](docs/DETAILS.md#limitations)).
Whether trading fees beat 2.2% for depositors depends on trading volume, which no simulation here includes.

## Limits

Every limit, with its file: [docs/DETAILS.md](docs/DETAILS.md#limitations). In short:

- Testnet only (Unichain Sepolia, mock tokens, our own small reference pool); not deployed on mainnet; no third-party
  review yet.
- Incentives are simulated. U-IC2 passed without a third-party refresher of the reference; with one, the cheapest
  clock reset and the smallest-deployment placement out-earn the reference curator at 120% volatility. In the gated
  runs (no refresher), the vault ends with more value under those two lax strategies (at 120%: about 558 and 569
  against 314, from 1,000), because time in range with no third-party depth is mostly loss to arbitrage.
- Market moves, positioning and token mix stay with depositors; skill is not paid.
- The reference TWAP is assumed not to be moved (moving it costs money, above); a single builder that wins every
  block of the 120 s window and censors arbitrage is outside the model.
- E5, E-IC and E0b: see [Results](#results).

## The proof

| | |
|---|---|
| Tests | 467 passed, 0 failed, 0 skipped: 301 unit, 73 attack, 44 invariant suite, 12 fuzz, 11 mainnet fork, 17 deploy, 9 eval (`audits/test-summary.json`) |
| Planted bugs | 332 mutants listed, 328 wired, every wired one killed by the test it names; 4 equivalent (`audits/mutants.json`) |
| Invariants | 13, in two pool states; 64 × 64 in `forge test`, 256 × 128 in `script/ci-local.sh`, plus a non-vacuity check |
| Static analysis | 215 Slither results, each with a verdict; none needed a code change (`audits/SLITHER.md`) |
| A second opinion | an independent Python model written from the specification; the contracts match its vectors bit for bit |
| Size | largest runtime 19,877 B against a 22,000 B budget (EIP-170: 24,576); checked by a real deploy |

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

E-IC historical supplement (one historical price series through the synthetic simulator, no third-party depth, point estimates, not a replay and not E-IC's rule): blocks 25,147,070 to 26,443,420; edge-12h net +11.282 / +1.123 ETH at NAV 1,850 / 185; gated bots edge-12h does not beat: none; E-IC stays NOT RUN. Form, data and attempts: `eval/E-IC-supplement-form.md`, `eval/supplements/E-IC-historical.json`, `eval/supplements/E-IC-hist/`.

Holding the deep pool's TWAP 60 ticks off costs about 1.04 WETH for one block, and about 7,466 WETH for the 24-hour cure
window (arithmetic); the reference curator's rebalancing charge and the full table: [DETAILS](docs/DETAILS.md#costs).

**Claims.** Every claim ID has a named test or a result file, and `script/claims.py` fails the build on a missing one.

- `CLAIMS_PHASE=P6 python3 script/claims.py` prints: "104/106 current claim IDs due by P6 covered + 3 historical
  failed (E0, E0a, U-IC; earlier configuration, final, never met) + 1 withdrawn (E0b; failed, claim withdrawn, never
  met)", with E5 (FAIL, control form) and E-IC (not run) missing.

## Contracts

| Contract | Holds | Runtime |
|---|---|---|
| `TouchstoneVault` | the tokens, one position, the curator's bank and bond; runs every action | 19,877 B |
| `TouchstoneVaultExtension` | the vault's rarer entry points (seed, claim, bond, retire, poke, breach processing) | 15,046 B |
| `TouchstoneHook` | the breach clock, the cached reference, a witness position, the records | 14,329 B |
| `TouchstoneFactory` | deploys all three together and seeds the vault | 6,416 B |

The hook is separate because a hook's own calls skip its own callbacks; the vault is split to stay under the
contract size limit. More: [docs/DETAILS.md](docs/DETAILS.md#how-it-works).

Testnet deployment (Unichain Sepolia, chain 1301, demo tier; the reference pool there is our own mock-token v3 pool, see
[Limits](#limits)): vault `0xA8FD240dEFEBE61200C67e8FBfbF8BFC4597b962`, hook `0x93700F94801eB5542313c87c22cd6c59e875e0A0`,
factory `0x68b4C7D0a58f66d4f8c49B493370824a191e1fE1`, curator bundler `0xeccbecA62ffCC13f78890BA9481f7D8649f6Ae31`,
reference pool `0x84517dBDaa924A8d9bc2abb97BAAB40A648EC972` (all in `script/demo/deployments-1301.json`, with the
transactions of `script/demo/demoflow.sh`; deployed 2026-11-20 with the frozen 2.2% base rate).

## Run

```
forge build --sizes src                                   # every runtime ≤ 22,000 B
export FORK_BLOCK_1=26443420 MAINNET_RPC_URL=<archive RPC> # fork tests pin this block; unset, they return early (PASS)
forge test                                                # unit, fuzz, manipulation, invariant (64 × 64), fork
CLAIMS_PHASE=P6 script/ci-local.sh                        # the CI steps, 256 × 128 invariants, non-vacuity
python3 model/reference.py --selfcheck                    # the independent reference model
python3 script/mutate.py --audit                          # the curated mutant list
```

## More

[docs/DETAILS.md](docs/DETAILS.md) (the full technical record) · [REVIEWERS.md](REVIEWERS.md) (five claims to check in
a few minutes) · [SECURITY.md](SECURITY.md) · [CREDITS.md](CREDITS.md). Prior art: implementation shortfall (Perold,
1988); deferral and malus of variable pay; loss-versus-rebalancing (Milionis et al.); passive-LP benchmarks.

Partner integrations: None.
