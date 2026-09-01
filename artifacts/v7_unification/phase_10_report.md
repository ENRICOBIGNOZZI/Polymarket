# V7 Phase 10 — Final Legacy Eradication

STATUS: REPOSITORY_GATE_PASS

SOURCE_CODE_SHA: `2a0521275d03aaff48329fb604b32e20679cd2d0`

SAFETY: `paper_only=true`, `authenticated_execution=false`, `real_order_submission=false`, `real_capital_at_risk=false`.

## Disposition

- Deleted the dormant standalone structural executor and hard-arbitrage guard. Their bounded rotation helper moved to the canonical structural research module; atomic completion, timeout, fee, full-depth, and bounded-unwind requirements remain in the structural engine contract and tests.
- Replaced the C++ structural detector's ledger-spool candidate path with native `polymarket_v7_opportunity_envelope_v1` proposals to `V7_GLOBAL_PORTFOLIO_COORDINATOR`.
- Moved fill-conditioned maker markouts from the ledger transport to the zero-authority research evidence plane and joined them back to canonical fills in both model fitters and monitoring.
- Deleted the unlaunched standalone maker runtime and its dormant PAPER execution-mode supervisor branch. Maker decision, queue, inventory, execution-policy, replay, fillability, and markout libraries remain tested without runtime economic authority.
- Reclassified the external-fair component into `BTC_SETTLEMENT_ENGINE`: native opportunity envelopes go to the coordinator; virtual lifecycle labels go only to research evidence and the durable counterfactual tape.
- Removed all 16 `DELETE_ACTIVE_LEGACY` local branch refs after proving each was an ancestor of `origin/main`. Two clean auxiliary worktrees were detached before their branch refs were removed. All prior objects remain recoverable from the rollback bundle.

## Machine-readable proof

- `artifacts/v7_unification/legacy_deletion_manifest.json`
- `artifacts/v7_unification/path_classification.json`
- `artifacts/v7_unification/authority_graph.json`
- rollback tag: `v7-unification-pre-migration-8d9e8e60`
- rollback bundle SHA-256: `6b1fe45289cbb0c14aff6e31960851d197e87f6fe0534d56518f73f9509e6eee`

Final static authority result: zero unexplained ledger-transport edges, zero P0/P1 migration defects, one declared owner per authority, two economic engines, and `target_topology_complete=true`. Surface classification contains zero `DELETE_ACTIVE_LEGACY` and zero `KEEP_TEMPORARY_COMPATIBILITY` entries.

## Validation completed in deletion waves

- Release C++ structural runtime build and `pm_fast_arb_tests`.
- Release maker observer build and all retained maker HFT/execution-cell/lane/PAPER/inventory/execution-policy tests.
- Native opportunity parsing and global coordinator tests.
- External-fair/BTC settlement component, RTDS monitor, runtime supervisor, monitoring, economic-loop, cutover, process-manifest, surface-classification, and authority-reachability tests.
- Python compilation and shell syntax checks for all touched runtime/deployment paths.

The full exact-SHA Debug/Release/sanitizer matrix and post-eradication bounded PAPER canary are Phase 11/12 final verification work. Physical PAPER-host inspection/cutover remains an `EXTERNAL_BLOCKER` because SSH owner authentication is unavailable; GitHub ruleset administration remains an `EXTERNAL_BLOCKER` without repository-admin credentials.
