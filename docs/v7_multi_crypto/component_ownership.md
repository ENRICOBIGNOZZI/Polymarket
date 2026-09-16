# V7 Multi-Crypto — Component Ownership

Observed/updated: 2026-09-17. This document describes authority, not just
process names. It does not activate a new lane.

## Canonical authority

- `V7_GLOBAL_PORTFOLIO_COORDINATOR` remains the single owner of capital
  admission, portfolio risk, OMS intent and inventory arbitration.
- `ledger_router` remains the single process allowed to open and append the
  canonical `ledger/execution.jsonl` writer.
- Strategy, feature, book, oracle and simulator components may propose facts or
  actions. They do not acquire independent capital or ledger authority.
- PAPER execution adapters act only after a coordinator decision. They are not
  permitted to create a second allocator or to bypass receipt validation.

## Frozen production cohort

The active BTC M5 `LEAD_LAG_TAKER_V1` remains on the historical frozen SHA and
protocol. Its REST/book reread and file-receipt path are legacy cohort semantics.
They are measured and audited, not silently replaced in place.

The current maker executor is likewise subordinate to coordinator authorization.
No refactor in the isolated multi-crypto branches changes the active launcher,
process manifest, BTC config or production process set.

## Parallel data-plane workstream

The isolated `research/v7-multi-crypto-lead-lag-20260916` worktree owns:

- asset/contract discovery and capability registry;
- parameterized external venue hub;
- Polymarket local book selection/hub;
- oracle/reference capture;
- six-asset SHADOW feature tape and causal repricing labels;
- London bootstrap/benchmark preparation.

These components remain SHADOW/observation surfaces until an explicit protocol
freeze and the shared execution/accounting gates are satisfied. Their existence
does not authorize ETH/SOL/XRP/DOGE/BNB PAPER entries.

## Execution/accounting workstream

Draft PR #960 owns complementary, non-deployed infrastructure:

- deterministic FAK/latency/capacity replay;
- global reconciled PAPER cash checkpoint;
- durable shared reservation projection;
- bounded direct coordinator IPC;
- durable IPC request/ACK to the existing ledger writer;
- lead-lag accounting audit and monitoring/risk reconciliation;
- pooled multi-crypto residual benchmark;
- in-memory C++ PM-book to existing taker-simulator bridge.

## Lifecycle ownership

| Stage | Owner | Authority |
|---|---|---|
| Discovery/rules | registry/data-plane | evidence only |
| Venue/oracle/PM books | shared hubs | evidence only |
| Features/model | feature/residual components | signal only |
| Candidate | strategy component | proposal only |
| Capital reservation | global coordinator | single shared authority |
| Risk/market arbitration | global coordinator | single shared authority |
| Submit lifecycle fence | global coordinator | single OMS owner |
| PAPER fill simulation | authorized execution component | mechanical, receipt-gated |
| Canonical append | ledger router | single writer |
| Settlement observation | settlement/evidence component | evidence only |
| FINAL/accounting view | canonical ledger/economics | no new execution authority |

## Integration invariants

A new lane must bind checkpoint SHA, runtime SHA, candidate identity, coordinator
receipt and canonical writer ACK. Old evidence may seed a new checkpoint but is
never relabeled as new-runtime evidence. Queue-full, stale checkpoint, missing
receipt, unknown fee/rule/book, unresolved settlement or writer failure blocks
new entry. Risk-reducing reconciliation and settlement remain allowed.

No branch in this work has real-order authority or automatic promotion.
