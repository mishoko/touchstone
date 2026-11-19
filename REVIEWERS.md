# For reviewers

Touchstone is a Uniswap v4 hook vault that charges each curator action its measured cost, at the worse of spot and
the 10-minute TWAP of a deep Uniswap v3 pool, out of the curator's unvested pay and bond, and reverts an action they
cannot cover. A delayed settlement against the reference TWAP closes the lag between that price and the market in
expectation, and a curator who leaves the vault out of range for 24 hours of observed time forfeits its unvested pay
(three times: removal). Market moves, positioning and token mix are not covered, and the limits are listed in the
README and SECURITY.md.

## Five claims to check

Run from the repository root after `forge build` (the first build compiles with via-IR and takes several minutes).
Each command prints `[PASS]` for every matched test; `-vv` shows the logged numbers.

1. **An action the curator cannot cover reverts, and depositors can still withdraw.** The test builds the worst legal
   placement, shows charge plus reserve fits the bond twice, passes with the bond at exactly the burns, and reverts
   `Insolvent` with one share less.

   ```
   forge test --mt test_U_solv__worstPlacementPlusReserveFitsTheBond
   ```
   Expected: `[PASS] test_U_solv__worstPlacementPlusReserveFitsTheBond()`.

2. **A self-sandwich around a placement loses money; with the defence removed it profits.**

   ```
   forge test --mt 'test_M_N4__(gridLoses|ctlReferenceFollowingThePushProfits)' -vv
   ```
   Expected: two `[PASS]` lines; the grid logs `max net (wei)` ≤ 0 for each of three states (−11789874218719271, 0,
   0) with 27 / 36 / 27 rows acted and charged; the control logs `M-N4 control net (wei) 15462104634183963`.

3. **The markout settlement closes the lag lane: placing at a stale price with a colluding trader loses; without the
   settlement's legs it profits.**

   ```
   forge test --mt 'test_M_2d__(lagLaneGridLoses|ctlLagLaneWithoutMarkoutProfits)' -vv
   ```
   Expected: two `[PASS]` lines; `M-2d lag grid: rows acted and charged 64 of 72`;
   `M-2d lag-lane control: best net (wei) 182668154910845142`.

4. **A charge is never netted: a gain at one price does not offset a loss at another, or a later charge; holdings that
   gain at every price are charged zero.**

   ```
   forge test --mt 'test_U_net__'
   ```
   Expected: `[PASS] test_U_net__aGainNeverOffsetsAChargeOrALaterCharge()` and
   `[PASS] test_U_net__gainAtEveryLegIsChargedZero(...)` (a fuzz test, 256 runs).

5. **Three breaches remove the curator: deposits then revert `BadMode`, and every depositor can withdraw in full.**

   ```
   forge test --mt test_U_breach__anEmptyVaultReachesExitedAndWithdrawalsStayOpen
   ```
   Expected: `[PASS] test_U_breach__anEmptyVaultReachesExitedAndWithdrawalsStayOpen()`.

All tests: `forge test`. Fork tests need `MAINNET_RPC_URL` and `UNICHAIN_SEPOLIA_RPC_URL`; without them they return early
and print PASS without running. Numbers quoted from them use `FORK_BLOCK_1=26443420`. Evaluation results: `eval/results/`, summarised in the README.

## Files to read first

- `src/TouchstoneVault.sol`: every curator action, the charge and the settlement (`_action`, `_charge`, `_settle`).
- `src/libraries/ExecLedger.sol`: the worse-of valuation and the settlement legs.
- `src/libraries/FeeBank.sol`: unvested pay, vesting, the bond, forfeiture and the solvency gate.
- `src/TouchstoneHook.sol`: the reference cache, the breach clock and the records.
- `test/manipulation/CellBase.sol`: how every attack cell measures the coalition's net and its control.

## Open issues

- E5 is FAIL (control form) under its registered rule: N1 and N2 have inline sensitivity checks, not separate control
  tests. Every asserted attack row nets ≤ 0.
- Reported, not asserted, positive rows: the capped markout tail after the vault's own move, protocol-fee and off-leg
  stops, and boundary parking while the cached reference lags (README, Limitations).
- A single block builder holding the reference over the markout window is outside the model (safe NAV about 150 WETH).
- Incentives are measured on synthetic paths only; with an outside observer refreshing the reference, two lax
  strategies out-earn the reference curator at 120% volatility. The historical replay (E-IC) was not run.
- The allocation ledger carries no closure claim (E0b failed).
- The NAV cap is checked on the NAV priced at the last action, so a vault left unplaced through a large move can exceed
  it (about +25% after a 50% move with half the NAV in token0).
- Withdrawals while a settlement is pending exit at the pre-settlement price.
- The testnet demo uses our own mock-token v3 reference pool and a provisional 1% base rate (2.2% is set at the
  parameter freeze).
- The contracts are not audited.
- Two small disclosed effects of supply changes: a supply change inside a markout window moves the settlement's burn
  (`test_U_settle__inWindowDripBurnOverBurnsTheCuratorsReserve`); a holder who leaves during a reference outage skips
  its share of the deferred base (`test_U_base__deferredBaseSkipsALeaverDuringTheOutage`).
