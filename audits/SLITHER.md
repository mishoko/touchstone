# Slither triage (test S-1)

- Run: `slither . --filter-paths 'lib/|test/|script/' --exclude-dependencies --json output/s20/slither.json`.
- Tool: Slither 0.11.5, solc 0.8.26, via-IR.
- Scope: `src/` (64 contracts analysed, 101 detectors).
- Result: 215 results, triaged below. None requires a code change.
- Verdicts:
  - **FP**: a false positive (the detector's premise does not hold here).
  - **Design**: the flagged pattern is intended and covered by the named test.
  - **Accepted**: a real observation with no effect on funds, documented here.

## High (10)
| Detector | Count | Where | Verdict |
|---|---|---|---|
| `uninitialized-state` | 6 | `VaultStorage._lo`, `_hi`, `_epoch`, `_tAction`, `_seeded`, `curator` | **FP.** The vault and its extension share one storage layout (`VaultStorage`). These slots are written by the vault (`_action`: `(_lo, _hi)`, `_tAction`) or by the extension's code under `delegatecall` (`seed`: `curator`, `_seeded`, `_epoch`; `roll`: `_epoch`). Slither analyses each contract alone and cannot see writes across the `delegatecall`. The storage-layout snapshot (`test/storage/`) and U-ext pin the shared layout. |
| `controlled-delegatecall` | 1 | `TouchstoneVault._delegate` | **Design** (spec §4.12, the vault/extension split). The target is the immutable `EXTENSION`, whose code hash the factory checks at deploy. Only the vault's own fixed selectors reach it, and the extension's entry points run only by `delegatecall` (`delegated`). U-ext (unknown selectors, direct calls, the dispatch table) covers it. |
| `arbitrary-send-erc20` | 2 | `VaultCommon._payIn`; `CuratorBundler.topUpBond` | **FP.** `_payIn`'s `from` is the vault itself at seed, or the depositor recorded as the unlock's payer, which is `msg.sender` of `deposit` (`TouchstoneVaultExtension.sol:251-252, 307-308`). The bundler pulls only from its immutable owner, in an `onlyOwner` function. |
| `encode-packed-collision` | 1 | `TouchstoneFactory.deploy`: `abi.encodePacked(hookCreationCode, abi.encode(hookArgs))` | **FP.** This is the standard CREATE2 init-code concatenation. The factory checks `keccak256(hookCreationCode)` against the immutable `HOOK_CODE_HASH` before the call, so the code part is fixed and no other split of the bytes can pass the check. |

## Medium (81)
| Detector | Count | Verdict |
|---|---|---|
| `reentrancy-no-eth` | 11 | **FP.** Every external call is to the immutable v4 `PoolManager` (inside the vault's own `unlock`), to the immutable hook, to the factory from the bundler, or to the pool's two ERC-20 tokens (`_payIn`, the withdrawal's payout; the deploy script allow-lists the tokens, and fee-on-transfer, rebasing and callback tokens are unsupported, spec §0.1). The PoolManager calls back only through `unlockCallback`, which requires `msg.sender == PoolManager` and an active entry (one level; a nested unlock reverts). The vault's entry points are `nonReentrant`. U-reent covers it, including foreign unlocks and each entry. |
| `unused-return` | 38 | **Design.** The ignored values are tuple members the caller does not need: `getSlot0`'s unused fields, `modifyLiquidity`'s fee delta (collect-first books it separately), the `unlock` return data, `settle()`'s paid amount (the balance delta is checked), and `bankApply`'s unused outputs. Two are deliberate probes: the hook constructor's `observe` is a history check that reverts on failure (D-5), and `initialize`'s returned tick is unused. |
| `uninitialized-local` | 22 | **Design.** The locals default to zero by intent: accumulators, flags, and optional outputs that are set on one branch. |
| `incorrect-equality` | 7 | **Design.** Strict equality on counters and anchors: `amount == 0`, `totalSupply() == 0`, `_refGrid + 1 == k` (consecutive anchors), the snapshot's same-block check, `tA == 0` (no pending action). No balance that a third party can move is compared for equality. |
| `divide-before-multiply` | 3 | **Design.** `c / TICK_SPACING * TICK_SPACING` and `x / GRID * GRID` floor to the grid on purpose. Negative ticks are corrected after the division (`witnessStep`); the grid indices are unsigned. |

## Low (27)
| Detector | Count | Verdict |
|---|---|---|
| `reentrancy-benign`, `reentrancy-events` | 13 | **FP**, for the same reasons as `reentrancy-no-eth`. Events emitted after a PoolManager call carry values computed inside the same entry. |
| `timestamp` | 8 | **Design.** The reference windows (TWAP anchors, the markout window) are defined in seconds by the Uniswap v3 oracle. Block-time skew moves a window by at most one block; the fence and the markout legs bound its effect (spec §3.3, §3.4). |
| `missing-zero-check` | 4 | **Accepted.** The four inputs are deployer-supplied: the bundler's owner, the factory's PoolManager, the vault's extension (the factory computes it), and the seed's curator. A zero curator gives a vault whose curator can never act or claim. The vault is then a lazy vault: the clock breaches it and depositors exit in kind. No funds are at risk, and the official deploy scripts never pass zero. |
| `events-maths` | 2 | **Accepted.** `ackBreaches` and `fundWitness` are vault-only bookkeeping calls. The vault emits `BreachProcessed` for every acknowledged breach. `fundWitness` runs once, at seed, with the witness amounts from the seed parameters, which are in the factory's `deploy` call data. The live witness's liquidity is in `WitnessRolled`. |

## Informational and optimisation (97)
| Detector | Count | Verdict |
|---|---|---|
| `naming-convention` | 72 | **Accepted.** Parameter and immutable names follow the specification (e.g. `FORFEIT_DRIP_BLOCKS`, `m_perBlock`). |
| `constable-states` | 9 | **FP.** The same cross-`delegatecall` writes as `uninitialized-state`. |
| `unused-state` | 7 | **FP.** These are shared-layout slots used by the other half of the vault/extension pair. `DEAD_ADDRESS` is used by the extension's seed. |
| `assembly` | 4 | **Design.** The reference read with a fixed return-data size (`_window`, U-ref-fail: return bomb, gas bomb), the revert bubbling, CREATE2, and a 512-bit multiply. |
| `cyclomatic-complexity` | 3 | **Accepted.** `_checkpoint`, `_action` and `dispatch` follow the spec's binding step order (§4.6). |
| `low-level-calls` | 1 | **Design.** The `delegatecall` above. |
| `missing-inheritance` | 1 | **Accepted.** `TouchstoneVaultExtension` implements `IVaultEntries`'s selectors behind the vault's fallback. `script/ext_check.py` checks the selector set. |
