# Historical canonical accounting

The live decision report keeps current-run cash separate from archived generations. Restarting at a new code SHA must not make previous closed positions disappear from the evidence available to the operator.

`v7_profit_attribution.py --archive-root runs/paper_v7_archives --output history.json.gz` discovers each cutover's retained ledger and all compressed canonical checkpoints. It verifies PAPER authority and exact record identities, deduplicates identical `(model_sha, record_id)` records, and rejects conflicts or incomplete sealed records. Including all checkpoints avoids assuming that the largest file contains every historical event. Missing ledger sources are listed explicitly in the archive inventory.

The existing reporting worker refreshes `profit_attribution_history.json.gz` every ten minutes. The compressed report preserves position-level attribution and source hashes. `economic_decision_report.json` and `NEXT_ECONOMIC_ACTION.md` include a compact summary by code generation and component. Historical totals do not enter current portfolio equity, current-run PnL, policy selection, or model promotion. An archive refresh failure preserves the previous report; its recorded timestamp remains visible.

Legacy positions without their original fills remain non-reconciled. Unknown gross PnL, costs and economic notional remain unknown. No fill quantity or probability is inferred from a final cash result.

The historical funnel currently covers ledger-observed stages. Archived coordinator and rejected-authorization streams are explicitly outside that funnel's coverage. This change does not attest a complete historical opportunity funnel or full historical ML datasets.

## Verified source audit

On 2026-09-09 the archive command read 709 ledger/checkpoint sources in 21 cutover directories. It recovered 547 final positions over 17 code generations; 546 reconciled. One legacy final of −2.00000031 lacked recoverable position fills in those sources and remains unidentified. The aggregate historical accounting result spans different code and research generations and is not a current-strategy profitability claim.

Tests cover compressed-only generations, duplicate checkpoints, conflicting records, incomplete sealed sources, archive path safety, JSON serialization, and preservation of unknown legacy economics. The full 143-entry Release, Debug and ASan/UBSan suites passed before the final serialization correction; focused regression tests also exercise that correction. Exact-commit CI and deployment of this follow-up remain required.
