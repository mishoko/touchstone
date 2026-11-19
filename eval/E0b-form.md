# E0b: frozen form

E0b is the pre-registered successor to E0 and E0a (both published as failed, earlier configuration). It tests the
allocation ledger of spec §3.8 with one changed input, the vault's fee leg, on fresh scripts that stress the remaining
approximation, the witness fee rate `f_w`. E0 and E0a are not superseded. This file's §2–§5 are pinned by sha256 in
`eval/E0b-frozen.json` before any data is evaluated.

## 1. Why
E0a failed on one epoch of its `exit` run (0.556 bp against a 0.3 bp bound). The residual equalled the vault's own fees
from an in-block crossing under a stale reference cache, which the ledger's time-based fee leg booked as zero. E0b reads
the vault's fees exactly and asks whether what remains, the passive leg's time-based fee rate, closes on scripts built
to stress it.

## 2. The ledger under test
E0a's ledger (`eval/E0a.py`, unchanged), with the vault's fee leg replaced:
```
fee_j  = feeV_j − L_b,j·Δτ_b,j·f_w
feeV_j = floor(L_j·Δg0/2¹²⁸)·P(p_j¹) + floor(L_j·Δg1/2¹²⁸)
Δg     = getFeeGrowthInside(lo_j, hi_j) read immediately before the closing vault call
         − the same read immediately after the opening vault call  (mod 2²⁵⁶)
```
`(L_j, lo_j, hi_j)` come from the closing `Checkpoint`. L is constant within a segment (every vault entry checkpoints
first), so `feeV_j` equals the position's fees over the segment. The read is an evaluator input recorded by the harness
(`E0b-<tag>-feereads.jsonl`); no contract changes. An off-chain evaluator needs pool state at transaction boundaries
(an archive node's per-transaction pre-state or a trace), not block-end state, when other transactions touch the pool in
the same block. The passive leg is unchanged: `L_b,j·Δτ_b,j·f_w`, with `f_w` the witness's fees over its lifetime
divided by Δτ_b over the lifetime. On the fit data, the `exit` row that failed E0a closes by construction (the read is
the truth's own quantity); it is not evidence.

What remains is the passive leg's error: fees attributed to segments in proportion to credited time rather than to when
they were earned. It is zero when `L_b/S` is constant over a lifetime and the lifetime coincides with the epoch window;
it is not zero when (1) `L_b/S` changes inside a lifetime (re-placements), (2) a lifetime and an epoch window differ
(an epoch whose first vault call is not the poke), or (3) witness fees accrue in uncredited time (a stale cache, an
unobserved span, a crossing held outside the range) and (1) or (2) applies.

## 3. Runs
### 3.1 Fit data (reported, not gated)
E0a's three scripts `same`, `offset`, `exit`, run verbatim by `test/eval/E0bData.t.sol` (run names unchanged: they key
the walk). Their truth files are byte-identical to E0a's and their logs identical up to the vault's address (the vault's
creation code carries a metadata hash over source comments changed since E0a's run; the address appears only in
`Transfer` topics). E0a's six data files are pinned in `eval/E0a-data.sha256`.

### 3.2 Held-out scripts (gated)
- Generator `eval/e0b_gen.py <seed>`; seeds 1100–1107 (eight scripts). Seeds below 1100 are spent and used only for the
  validity smoke. The held-out run name is `e0b-<seed>`.
- Shape: epoch 0 a poke at +22 and a placement at +25 at offset O from the live witness, O ∈ {−600, −300, 0, +300,
  +600} ticks; epochs 1–12 as drawn below; then closing pokes at +22 of epochs 13 and 14. TEST tier (EPOCH 120 blocks).
  The third-party walk is E0a's (liquidity 1e21 on ±6,000 ticks), around the fixed tick 100 ± 80 by epoch parity,
  with a per-20-block intensity: 0 = refresh only, 1 = E0a's 20–30-tick steps within ±60, 3 = 60–90-tick steps within
  ±180.
- Draw list, per epoch, from one `Random(seed)` stream, every parameter drawn whether or not its op is drawn:
  (1) non-poke-first, p 0.4; (2) its tail length 20–40 and poke block +40…+70; (3) re-placement, p 0.6, its offset
  O′ and block +50…+90; (4) stale window, p 0.5, start +10…+60, length 45–80; (5) edge crossing, p 0.4, block
  +30…+100, edge (lower or upper), depth 150–400 ticks, hold 5–10 blocks; (6) rebalance, p 0.2, block +30…+100, size
  0.5–1.0% of NAV, direction (the harness uses the token whose idle holds more value); (7) unobserved span of 60
  blocks, p 0.2, start +30…+100; (8) other holder's flow, p 0.3, block +30…+100, deposit or withdraw, deposit size
  1–20% of S, withdraw size 10–100% of the holder's shares (a withdraw with none held becomes the deposit); (9) claim,
  p 0.2, block +30…+100; (10) the walk intensities, one per 20-block sub-window, each from {0, 1, 3}. Epoch 0 draws O
  first.
- Ops: a non-poke-first epoch is a settle at +2, the walk at intensity 3 until +2 + tail, then the poke (moved after
  the tail if drawn earlier); a poke-first epoch pokes at +22. A crossing takes spot past the edge in one block, holds
  it there with no touch, then returns in one block to the walk's target. A stale window makes reference reads fail
  while the walk continues (intensity at least 1). An unobserved span advances with no touch.
- Clamps, after all draws (so a clamp never shifts the stream): the poke (and a non-poke-first settle and tail) are
  anchors; optional ops are placed in draw order on disjoint block intervals, shifted past a conflict (vault calls also
  `COOLDOWN` = 14 blocks apart; a re-placement and a rebalance at least `COOLDOWN` after the poke), and dropped unless
  they end by +110; per epoch, pre-swaps (1.5% of NAV each, worst case) and the rebalance stay under 4% of NAV
  (`MAX_SWAP_EPOCH` = 5%). Presence by construction, to a fixed point: if no non-poke-first epoch, re-placement, stale
  window or crossing survives the clamps, one is forced into the first epoch from epoch 2 where it survives as drawn;
  else where it survives once that epoch's rebalance, unobserved span, flow and claim are dropped; else where it survives
  once a re-placement, stale window or crossing that also survives in another epoch is dropped; else a required op
  whose only survivor blocks the epoch is first moved to another epoch (dropping only that epoch's rebalance,
  unobserved span, flow and claim) and then dropped from it. Every clamp is logged in the script.
- Legality in the harness: a placement or rebalance first moves spot within the fence of the live reference by a
  third-party swap (as E0a's placements do); a rebalance is capped at 90% of the input token's idle. There is no
  clamp keeping a placement around the walk's target; a placement the vault rejects (e.g. `NotContained`, `DeltaMin`)
  is a reverted call. A vault call that reverts is recorded with its reason and its discarded logs, writes no truth,
  fee-read or log row, and the script continues. Outcomes of a run: a script that reverts, runs past 1 h, or that the
  generator cannot produce is a failed script; a run killed by a signal, or failing for a tool or compiler reason (the
  build runs first, untimed), is void and re-run once with the owner's approval.

## 4. Pre-registered rule
### 4.1 Verdict
E0b PASS iff, on the eight held-out scripts:
1. every epoch row closes: |residual| ≤ 0.3 bp of V (the fixed depositor's value at the window's start); rows that
   §3.8 leaves unresolved are excluded here and counted by condition 3;
2. every share row closes exactly (the record's minted equals the vault's `Transfer` mints; the recomputed burns equal
   its burns);
3. no script has more than two epochs that §3.8 leaves unresolved;
4. the non-vacuity conditions of §4.2 hold;
5. every gated control (§5) was shown before registration to exceed the bound on a closing fit row, except the stated
   `prev_fw_whole_window` fallback.

Any failed script (incomplete, or more than 25% of its vault calls reverted) makes E0b FAIL. The verdict is one
judgement over all eight scripts; no subset, no re-draw. The fit rows are reported next to it.

### 4.2 Non-vacuity (from the truth and the witness series, never from the residual)
- Every script: at least one non-poke-first epoch; one re-placement inside a lifetime (a registration after the
  lifetime's roll with L > 0 before it); one stale window and one crossing that count: ΔtauB < Δb − 4 between the
  first-touch rows of their opening and closing touched blocks.
- Each of the following separately in at least 6 of 8 scripts:
  (a) some epoch's true passive fees `Σ_j (D/S_j)·L_b,j·φ_j` ≥ 3 bp of V (φ_j from the witness reads after the opening
      call and before the closing call on the segment's own witness, valued at the segment's p¹);
  (b) some lifetime has two segments, each with at least 10 τ_b-credited blocks and non-zero witness fees, whose fee per
      credited block differs by at least 2×;
  (c) some lifetime has at least 10% of its witness fees in block spans with ΔtauB = 0.
- |alloc| ≥ 3 bp of V in some epoch of at least 4 of 8 scripts.
- Block spans: from the first-touch row of a touched block to that of the next touched block. The first-touch row is
  the block's `step` row (written after the refresh, before any swap), or, when a vault call is the block's first
  touch, its `pre` row for fee growth and its `post` row for `tauB`. A span whose witness range changes is excluded.

## 5. Controls
Each gated control must push a closing fit row above 0.3 bp before registration:
`no_alloc`, `lvr_price_leg`, `no_share_terms`, `drip_sign_flipped`, `single_forfeit_drip`, `witness_shift_one_spacing`,
`anchor_closing_price`, `fallback_latest_write` (E0a's); `vault_fee_by_tau` (E0a's time-based vault leg);
`vault_fee_opening_L` (the read multiplied by the opening checkpoint's L); `fee_by_n` (blocks in place of Δτ_b in
`f_w`); `no_passive_fee`; `prev_fw_whole_window` (the previous lifetime's `f_w` on every segment of a non-poke-first
window). If `prev_fw_whole_window` lacks power or its row does not close on the fit data, the frozen record states that
the second channel has no powered logs-only control, and no other candidate is tried. Reported, not gated:
`epoch_indexed_witness`. Controls are computed from the logs and the fee reads only.

## 6. Freeze and order
- Branch `e0b`. The frozen closure is the Solidity import closure of `test/eval/E0bData.t.sol`, `foundry.toml`,
  `remappings.txt`, `eval/e0b_gen.py`, `eval/e0b_run.py`, `eval/E0b-form.md`, `eval/E0a-data.sha256` (the harness
  closure H), plus `eval/E0b.py` and `eval/E0a.py`.
- All E0b data come from `eval/e0b_run.py`, which requires a clean tree before and after `forge test` and writes a
  manifest (commit, statuses, submodules, H's hashes, seed, script hash, environment, data-file hashes).
- `prefit` (H recorded) → validity smoke on spent seeds 0–19 (no residual; acceptance: all complete and present,
  ≥ 19/20 for each of (a)–(c), ≥ 16/20 for alloc) → logged changes only of four kinds (legality clamps, stress raises
  after a shortfall, measurement fixes, conformance fixes; the last two with a fixture that fails before and passes
  after; never in `src/`, `lib/` or `script/`) → `prefit2` (H and the evaluator recorded) → fit evaluation and control
  powers → candidate R (`registered: false`) and its review → registration: the next commit, changing only
  `registered` and `reviewed`, reported to the owner before any held-out seed → the single run in a worktree detached at
  the registration commit; each manifest committed on `e0b` → `verdict` → published either way.
- A finding after registration has no route: E0b is then FAIL or not registered. A held-out seed interrupted before its
  manifest is committed is re-run once with the owner's approval; a seed that cannot finish within 1 h, or a set not
  finished by Nov 24, is FAIL.

## 7. Wording
- PASS: "the allocation ledger, with the vault's fee leg read from pool state at each vault call, closes within 0.3 bp
  of a fixed depositor's value per epoch on 8 pre-registered scripts that stress the witness rate (TEST tier)".
- FAIL: "H, the ledger's closure claim is withdrawn and it stays disclosure with no guarantee; no third attempt"; E0b is
  published as failed with its numbers.

## 8. Scope
E0b tests that the events plus the per-call fee reads reproduce the state under a non-uniform fee rate, flows,
re-placements, late pokes, stale and unobserved spans. It does not test the benchmark's economic meaning or
manipulation. TEST tier, with `m_perBlock` raised as in E0a (disclosed).
