# V7 Multi-Crypto Lead/Lag — Task Manifest

| Milestone | State | Gate / current fact |
|---|---|---|
| M0 Audit and integrity | IN_PROGRESS | Runtime identity and frozen BTC verified; deeper ledger-history recovery audit still pending. |
| M1 Core common | IN_PROGRESS | Generic venue-symbol factory and fail-closed asset registry under implementation. |
| M2 Data plane | NOT_STARTED | Must provide PM book in RAM for new lanes and verified resync semantics. |
| M3 Execution/accounting | NOT_STARTED | Must retain one global coordinator/execution owner/ledger. |
| M4 Speed | NOT_STARTED | Profile before replacing file IPC/REST in the new lane. |
| M5 Research | NOT_STARTED | Normalized shocks, leadership, derivatives, cross-crypto, residual model. |
| M6 Multi-crypto forward | BLOCKED | ETH/SOL remain SHADOW until protocol freeze and M0-M5 gates. |
| M7 Breadth | BLOCKED | XRP/DOGE/BNB and M15 need discovery/rules/data gates. |
| M8 Capacity/readiness | NOT_STARTED | PAPER/shadow only; no real-money promotion. |

## Non-negotiable invariants

- Frozen BTC forward is not edited, restarted or silently redefined.
- `paper_only=true`.
- `authenticated_execution=false`.
- `real_order_submission=false`.
- `real_capital_at_risk=false`.
- No automatic promotion.
- No new lane receives entry authority by configuration default.
- One global risk/execution authority and one canonical ledger writer.
- Missing market/rules/feed/oracle information is `BLOCKED`/`UNKNOWN`, never zero or false evidence.
