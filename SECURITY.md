# Security

## Scope and trust model

- **No admin, keeper, operator or off-chain feed.** The one external input is one immutable Uniswap v3 pool's
  oracle (`observe`; never its `slot0` price): the 10-minute TWAP, and a 120-second window after each curator action for the
  markout settlement. Anyone can call `poke`, `processBreach`, `settle`, `refreshRef` and `snapshotIfNewBlock`.
- **Block building is assumed competitive.** Each block in which an attacker holds the reference pool's price off the
  market costs it fees or that block's arbitrage value. Under that model the markout is safe up to the tier's NAV cap
  (1,850 WETH on the mainnet tier). A single builder that wins the action's block and every block of the window,
  censors arbitrage on the reference pool and unwinds once is outside the model; against it the safe NAV is about 150
  WETH.
- **The reference TWAP is assumed not to be moved.** The measured cost of moving it is below (K-ref, E8).
- **Testnet.** The Unichain Sepolia demo uses our own mock-token v3 reference pool; the evidence for the reference is
  the mainnet-fork test K-ref (`test/fork/KRefFork.t.sol`), not the testnet.
- The contracts are not audited. This file and the tests are the evidence.

## Threats, mitigations and regression tests

Tests are named `test_<ID>__<claim>` or `invariant_<ID>__<claim>` (dashes as underscores); the file column says where
they live. Run one row with `forge test --mt '_<ID>__'`. Fork tests (K-1, K-ref, gas) return early and print PASS when
`MAINNET_RPC_URL` is unset; set it to run them. An attack row ("M-") asserts that the attack loses money and has a
positive control that profits with the defence removed, except N1 and N2, which have inline sensitivity checks (E5,
FAIL by control form under its registered rule), and N3, a forgery cell with an inline discrimination check;
M-ADV3-move's and M-N1's reported rows are not asserted (open cells below).

| # | Threat | Mitigation | Tests |
|---|---|---|---|
| T1 | Same-block push at the valuation point | worse of spot and the reference TWAP | M-1 (`test/manipulation/MPush.t.sol`) |
| T2 | Push across a block boundary; a thin pool with dust snapshots | the reference TWAP and the fence; an unreadable reference locks the curator out | M-2, M-2b (`test/manipulation/MPush.t.sol`) |
| T3 | Curator wash-trading | the charge does not depend on fees; the allocation ledger is disclosure only | M-3 (`test/manipulation/MFees.t.sol`) |
| N1 | Pushing the checkpoint price to move pay | no time-in-range term in the charge; the base needs the pool price strictly inside and the cached reference in range | M-N1 (`test/manipulation/MN1.t.sol`); **open:** parking while the cached reference lags (below) |
| R11-V | Vesting while out of range or after an unobserved absence | vest gate: fresh cache in range, live reference in range, no open gap | U-vest-gate (`test/unit/VaultCheckpoint.t.sol`, `VaultCheckpointLive.t.sol`), U-vest-park (`test/unit/VaultLadder.t.sol`) |
| R11-O | Nobody observes, so an absence is not counted | observed time only; unobserved time earns no base and vests nothing | E8 (`eval/results/E8.json`); **open:** observer economics only partly measured |
| N2 | Forced checkpoints to escape the malus | the base amount does not depend on segmentation (≤ 1 wei per segment); a mint timed for a low NAV per share earns only the vault's return on the base from that block (disclosed) | M-N2 (`test/manipulation/MN2.t.sol`) |
| N3 | Forging the disclosure ledger by oscillation | endpoint LVR term; boundary medians move only at a measured cost (about 7.8 token units per moved median in the test vault, about 7e-3 of NAV); suspect and thin-pool flags | M-N3 (`test/manipulation/MN3.t.sol`) |
| N4 | Self-sandwich around a placement without a swap | 100% worse-of charge | M-N4 (`test/manipulation/MN4.t.sol`, `MPinned.t.sol`) |
| ADV-1 | Needle position for leverage | width exactly W; β is not in pay | M-ADV1 (`test/manipulation/MADV1.t.sol`) |
| ADV-3 | JIT self-dealing on the curator's own swap | 100% worse-of charge; spot read first | M-ADV3 (`test/manipulation/MADV3.t.sol`, `MPinned.t.sol`) |
| ADV-3-move | The vault's own move through the curator's JIT liquidity, then a markout against it, repeated across cooldowns | the move shares the per-action swap cap and counts toward the epoch cap; charge and markout as for any action | M-ADV3-move (`test/manipulation/MADV3.t.sol`); **open:** the capped tail (below) |
| N5 | Charge beyond unvested pay and bond | solvency gate (revert), the reserve included | U-solv (`test/unit/VaultAction.t.sol`, `VaultPlace.t.sol`, `CuratorBundler.t.sol`), U-settle (`test/unit/VaultSettle.t.sol`) |
| D2-lanes | Dust, full-range, idle or stale positions earning the base | δ weighting and width exactly W | U-base (`test/unit/VaultBase.t.sol`) |
| T5 | `donate` | reverts | U-6 (`test/unit/CuratorBundler.t.sol`) |
| T7 | `initialize` front-run; a foreign pool key | the vault initialises its pool in its constructor; pool-id check | D-1 (`test/unit/HookPool.t.sol`, `test/deploy/HookDeploy.t.sol`), D-2 (`test/deploy/Privilege.t.sol`) |
| T8 | First depositor; unsolicited transfers | seed plus dead shares; internal idle accounting | M-7, I-11 (`test/unit/VaultSafety.t.sol`) |
| T10 | Curator extraction | no withdrawal path for the curator; guarded actions only | I-5 (`test/invariant/VaultP2.t.sol`) |
| T11 | Witness ticks cleared; salt collision | per-epoch salt; read, add, remove | I-8 (`test/invariant/VaultP2.t.sol`) |
| T16 | Sniping a forfeiture or a base mint | the forfeit drips over one epoch (the hook's constructor enforces a non-zero drip); a checkpoint runs on every supply change; **disclosed:** a base deferred during a reference outage is minted later on the supply then outstanding, so a holder who leaves during the outage skips its share | M-10 (`test/manipulation/M10.t.sol`, `test/unit/Hook.t.sol`); `test_U_base__deferredBaseSkipsALeaverDuringTheOutage` (`test/unit/VaultBase.t.sol`) |
| T19 | A burn counted as return | NAV read after settlement | I-12 (`test/invariant/VaultP2.t.sol`) |
| T20 | Re-entrancy; exotic tokens | `nonReentrant`; token checks | U-reent (`test/unit/VaultSafety.t.sol`) |
| T21 | Range-registration spoofing | vault-only setter; immutable vault address | D-3b (`test/unit/Hook.t.sol`, `test/deploy/HookDeploy.t.sol`) |
| T22 | A stale snapshot or reference at an action without a swap | the vault calls `snapshotIfNewBlock()`; actions read the live reference | U-1b (`test/unit/VaultSafety.t.sol`), U-ref (`test/unit/VaultRef.t.sol`) |
| T13 | Gas griefing; long idle gaps | fixed ring buffer; one position; one witness; bounded catch-up | G-1, G-2 (`test/unit/HookGas.t.sol`), G-3 (`test/unit/PokeGas.t.sol`), G-4 (`test/unit/ActionGas.t.sol`), U-gap (`test/unit/VaultRecords.t.sol`, `VaultLadder.t.sol`) |
| F-015 | Push the pool price, then place or swap at the pushed level; once or repeatedly, timed to a favourable block | charge at the worst of the pre-move spot, spot, the reference TWAP and the markout legs (a window that opens one block after the action, ± the fee band), settled after the window; fence of 60 ticks; swap caps | M-2d (`test/manipulation/M2d.t.sol`, `MPinned.t.sol`), U-settle (`test/unit/VaultSettle.t.sol`), K-ref (`test/fork/KRefFork.t.sol`) |
| MK-1 | Hold the reference pool off over the markout window to shrink a settlement | the window excludes the action's block; under competitive building each held block costs fees or arbitrage value; NAV cap; **disclosed:** the single-builder case; a missed slot after the action block lets that block's closing tick weigh one block in the window | K-ref (`test/fork/KRefFork.t.sol`), M-2d (`test/manipulation/M2d.t.sol`) |
| MK-2 | Snipe the settlement burn | deposits revert `MarkoutPending` while an action is pending; withdrawals exit at the pre-settlement price (disclosed) | U-settle (`test/unit/VaultSettle.t.sol`) |
| MK-3 | Settlement never called, or the window leaves the oracle history | settlement is step 0 of every entry point and `settle()` is open; a failed read burns the whole reserve; withdrawals never revert | U-settle (`test/unit/VaultSettle.t.sol`) |
| MK-4 | Escape the reserve by breach, retire or claim | the reserve sits outside every bucket and follows at settlement; `releaseBond` settles a due reserve first; **disclosed:** a supply change inside the window moves the burn's value (below) | `test_U_settle__retireLeavesTheReserveToFollowAtSettlement`, `test_U_settle__releaseBondSettlesAPendingReserveFirst`, `test_U_settle__inWindowDripBurnOverBurnsTheCuratorsReserve` (`test/unit/VaultSettle.t.sol`) |
| F-024 | A third party sandwiches an honest placement | the curator's bundler re-aligns spot (≤ 10 ticks) inside its own transaction; `maxCharge` | M-2d grief lane (`test/manipulation/M2d.t.sol`) |
| R-1 | Moving the reference pool's 10-minute TWAP | deep pool; cost measured on a mainnet fork (below) | K-ref (`test/fork/KRefFork.t.sol`), E8 |
| R-2 | Holding the pool price off to block an honest curator while the clock runs | the curator meets the fence atomically; the clock reads the cached reference only | U-clock-ref (`test/unit/Hook.t.sol`) |
| R-3 | Dodging the clock or earning base by parking the pool price | the clock reads only the cached reference; the base needs the price strictly inside (≤ 2 hours of credit per exit when parked one unit inside, disclosed) | U-clock-ref, U-clock-park (`test/unit/Hook.t.sol`), M-N1 lane B |
| N4-edge | Self-sandwich around an edge placement | 100% worse-of charge | M-N4edge (`test/manipulation/MN4.t.sol`, `MPinned.t.sol`) |
| B-1 | Resetting the breach clock with a dummy action | only a real placement (contains spot and the reference, δ ≥ 0.5) or two consecutive in-range reference anchors start a new run; swaps never reset | U-breach (`test/unit/VaultCheckpointLive.t.sol`), U-breach-dodge (`test/unit/VaultLadder.t.sol`) |
| B-2 | A third party forces a breach | needs the deep reference outside the range for 24 h of observed time (cost: K-ref); holding the pool's own price off does nothing | U-clock-ref (`test/unit/Hook.t.sol`) |
| U-lazy | The curator under-manages | the mandate is a W-wide range containing the price; the breach ladder | U-IC2 (`eval/results/U-IC2.json`, PASS without an outside observer); U-IC (`eval/results/U-IC.json`, earlier 6-hour policy, FAIL) |
| R2-01 | Dust re-placement to reset the breach clock | registration needs δ ≥ 0.5; reset needs consecutive in-range anchors | U-breach-dodge (`test/unit/VaultLadder.t.sol`) |
| R2-02 | Deposit timed around a predictable forfeiture | the forfeit drips over one epoch | M-snipe-forfeit (`test/manipulation/MSnipeForfeit.t.sol`) |
| R2-03 | A breach lost, a cohort escaping, or one absence counted three times | one hook latch per cure window crossed; processed before vesting | U-breach (`test/unit/VaultCheckpointLive.t.sol`) |
| R2-06 | A diligent curator breached while blocked or unobserved | the curator can always meet the fence; the clock reads only the fresh cached reference | U-clock, U-clock-gap (`test/unit/Hook.t.sol`, `ClockVectors.t.sol`) |
| R2b-01 | The curator pauses its own clock with 1-wei swaps | placements are not action-capped; only real placements start a run | U-breach-capdodge (`test/unit/VaultLadder.t.sol`) |
| R2b-02 | Pre-breach base escaping forfeiture, or vested pay burned, via checkpoint timing | breach-first checkpoint; unminted base forfeited; vesting only at checkpoints | U-forfeit-quiet, U-forfeit-2gap (`test/unit/VaultLadder.t.sol`) |
| R2b-03 | The ladder stalls after the first breach | every registration needs δ ≥ 0.5 and starts a new run | U-ladder-stall (`test/unit/VaultLadder.t.sol`) |
| R2b-04 | A forced breach by crediting a stale idle gap | span credit truncated at the cache's staleness | U-clock-gap (`test/unit/Hook.t.sol`) |
| R1b-01 | Base on a stale NAV or δ after a large flow | re-base after every state change | U-epilogue (`test/unit/VaultAction.t.sol`, `VaultPlace.t.sol`) |
| R2-08 | A bond too small for the worst legal action | bond ratio ≥ 2 × the worst single action, the reserve included | U-solv (`test/unit/VaultAction.t.sol`, `VaultPlace.t.sol`) |
| R2-09 | A witness roll reverting an exit | the witness is owned by the hook, fail-soft, poke-only | I-10 (`test/invariant/VaultP2.t.sol`, `test/unit/VaultLiquidity.t.sol`) |

## Open cells and disclosed limits

1. **The vault's own move against a markout past the settlement cap (M-ADV3-move).** The reserve caps what one
   settlement can recover. Past the break-even markout (about 150–240 ticks depending on size, E5 logs) a colluding
   pair nets up to +0.93 token units in the test vault (about 8e-4 of NAV). Reported, not asserted
   (`test_M_ADV3_move__perDrawUncappedOnLegLoses` logs it).
2. **Arbitrage that stops off the settlement's legs.** With a protocol fee on, a rational arbitrage stops about 15
   ticks short of the market: +0.004 (`test_M_ADV3_move__protocolFeeStopsReported`); off-leg stops and both-worlds
   overshoot: up to +0.0006 (`…offLegStopsReported`, `…bothWorldsOvershootReported`).
3. **Boundary parking while the cached reference lags (M-N1 lane B).** Parking the pool price one unit inside the
   range while the cached reference still reads in range earns base for at most one anchor plus the TWAP's lag (2
   hours on the mainnet tier) per exit: up to +0.00023 per exit in the test vault on the test tier (about 11 minutes of
   credit at 1% a year); on the mainnet tier at most m·δ·NAV over 2 h ≈ 5e-6 of NAV per exit
   (`test_M_N1__parkingWhileTheCacheLagsIsBoundedByItsInRangeTime` asserts the bound, not the sign).
4. **Single block builder (MK-1).** Outside the model; safe NAV about 150 WETH against the 1,850 WETH cap. A missed
   slot after the action block lets that block's closing reference tick weigh one block in the window.
5. **Cost of moving the reference TWAP (E8, mainnet fork at block 26443420):**

   | Push | Hold one block | Round trip | Hold 24 h (arithmetic) |
   |---|---|---|---|
   | 10 ticks | 0.047 WETH | 0.048 WETH | 341 WETH |
   | 30 ticks | 0.294 WETH | 0.148 WETH | 2,113 WETH |
   | 60 ticks | 1.04 WETH | 0.298 WETH | 7,466 WETH |
   | 120 ticks | 3.85 WETH | 0.596 WETH | 27,714 WETH |

   The 24-hour figures assume arbitrage restores the price every block; a single builder pays about one round trip.
6. **A supply change inside a markout window moves the settlement's burn.** The burn is computed on the action's
   recorded supply: a drip or breach burn inside the window makes it over-burn the curator's reserve (holders gain); a
   base mint inside the window under-charges by at most the base accrued over 120 s
   (`test_U_settle__inWindowDripBurnOverBurnsTheCuratorsReserve`, `test_U_settle__baseMintedInsideTheWindowRaisesSAct`).
7. **A deferred base skips a leaver.** A base deferred during a reference outage is minted at the next priced
   checkpoint on the supply then outstanding; a holder who leaves during the outage skips its share
   (`test_U_base__deferredBaseSkipsALeaverDuringTheOutage`).
8. **The NAV cap is checked on the NAV priced at the last action**, so a vault left unplaced through a large move can
   exceed it (about +25% after a 50% move with half the NAV in token0).
9. **Withdrawals during a pending settlement exit at the pre-settlement price** (MK-2).
10. **A base mint timed for a low NAV per share** earns only the vault's return on the base amount from that block
    (`test_M_N2__aMintAtALowNavPerShareEarnsOnlyTheVaultsReturnOnTheBase`).
11. **Observer economics.** With an outside observer refreshing the reference every two hours, two lax strategies
    out-earn the reference curator at 120% volatility (`eval/supplements/U-IC2-observer.json`). The curator's
    recapture share of forfeit burns after removal, and five other operating numbers, were not measured (E8 lists them).
12. **E5's control form.** N1 and N2 have inline sensitivity checks rather than separate control tests; E5 is FAIL
    under its registered rule (`eval/results/E5.json`).

## Other evidence

- **Mutation testing:** 332 curated mutants (328 wired, 4 equivalent), each naming the test that must kill it; all
  wired mutants killed (`audits/mutants.json`, `script/mutate.py`). A lint binds every `require`/revert guard in
  `src/` to a mutant or a reasoned exception (`test/static/test_static.py`).
- **Invariants:** 64 × 64 in `forge test`; 256 × 128 with a non-vacuity check in `script/ci-local.sh`.
- **Static analysis:** Slither, findings triaged in `audits/`.
- **Independent reference model:** `model/reference.py`, golden vectors matched bit for bit.

## Reporting a vulnerability

Please use GitHub's private vulnerability reporting (Security → Report a vulnerability) on this repository. Use public
issues only for non-sensitive reports.
