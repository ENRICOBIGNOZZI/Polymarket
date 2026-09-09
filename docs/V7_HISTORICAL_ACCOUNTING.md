# Historical canonical accounting

The live decision report keeps current-run cash separate from archived generations. Restarting at a new code SHA must not make previous closed positions disappear from the evidence available to the operator.

`v7_profit_attribution.py --archive-root runs/paper_v7_archives --output history.json.gz` discovers each cutover's retained ledger and all compressed canonical checkpoints. It verifies PAPER authority and exact record identities, deduplicates identical `(model_sha, record_id)` records, and rejects conflicts or incomplete sealed records. Including all checkpoints avoids assuming that the largest file contains every historical event. Missing ledger sources are listed explicitly in the archive inventory.

The existing reporting worker refreshes `profit_attribution_history.json.gz` every ten minutes. The compressed report preserves position-level attribution and source hashes. `economic_decision_report.json` and `NEXT_ECONOMIC_ACTION.md` include a compact summary by code generation and component. Historical totals do not enter current portfolio equity, current-run PnL, policy selection, or model promotion. An archive refresh failure preserves the previous report; its recorded timestamp remains visible.

Legacy finals can reference their original fill through `canonical_maker_fill_record_id`. Attribution uses that explicit link only when code, fill, order, market, token, side and causal timestamps agree. A fill cannot be allocated to two final positions. The output records the join provenance and original position IDs; source records are unchanged. Positions whose fills are actually absent remain non-reconciled, and unknown gross PnL, costs and notional remain unknown. No quantity or probability is inferred from a final cash result.

The historical funnel currently covers ledger-observed stages. Archived coordinator and rejected-authorization streams are explicitly outside that funnel's coverage. This change does not attest a complete historical opportunity funnel or full historical ML datasets.

## Verified source audit

On 2026-09-09 the archive command read 708 archived ledger/checkpoint sources in 21 cutover directories. All 547 final positions over 17 code generations reconcile, with zero unattributed PnL. A legacy final of −2.00000031 had a fill with no position ID; its explicit canonical fill-record reference recovered the exact accounting relationship. The aggregate historical result spans different code and research generations and is not a current-strategy profitability claim.

Tests cover compressed-only generations, duplicate checkpoints, conflicting records, incomplete sealed sources, archive path safety, JSON serialization, explicit fill-reference identity and time checks, double-allocation rejection, and preservation of unknown legacy economics. All 143 CTest entries passed in Release, Debug and ASan/UBSan after these changes. Exact-commit CI and deployment of this follow-up remain required.
