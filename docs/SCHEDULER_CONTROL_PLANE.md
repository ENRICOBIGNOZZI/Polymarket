# Polymarket scheduler control plane

The project assigns each workflow one bounded responsibility and exactly one GitHub Actions job. The machine-readable source of truth is [`config/scheduler_registry.json`](../config/scheduler_registry.json). Research, evidence review, integration, validation, deployment and runtime supervision remain separate; no individual scheduler owns the complete chain.

## Administrator contract

The project administrator owns the evolution of the single live champion. Automation may collect evidence, enforce policy, merge a pre-approved integration and execute validation/deployment handoffs, but it may not create production authority for itself.

A model integration can be merged automatically only when all three labels are present:

- `approved-for-integration`: reusable implementation passed the research gate;
- `single-model-reviewed`: one orchestrator, allocator, risk state and broker remain;
- `administrator-approved`: the administrator authorizes this exact integration.

Removing any label removes automatic merge eligibility. No scheduler uses an administrative bypass.

## One scheduler, one responsibility

| Scheduler | Job | Responsibility | Explicitly forbidden |
|---|---|---|---|
| Administrator Supervisor | `supervise` | Observe champion, workflow health, PR queues and blockers | approve research, merge, dispatch validation, deploy |
| Research Policy | `enforce` | Enforce branch/label/manifest/shadow policy | select alpha, merge, deploy |
| Research Queue | `audit` | Inventory evidence and integration backlog | approve, merge, change live configuration |
| Integration Merge | `merge` | Verify the incumbent is fully deployed/healthy, then merge at most one fully green administrator-approved `integration/*` PR | evaluate research, run validation, deploy |
| Post-Merge Validation | `dispatch` | Dispatch CI, monitoring and live-paper validation for the exact merged SHA | merge, advance `paper-validated`, deploy |
| CI | `build-test` | Release/Debug build and deterministic tests | merge, deploy |
| Monitoring Validation | `validate` | Validate observability contracts | merge, deploy |
| Live-Paper Validation | `live-paper-smoke` | Validate public-data paper behavior and advance `paper-validated` on success | merge research, deploy arbitrary refs |
| Paper Server Deploy | `deploy` | Deploy only `paper-validated` and verify services | select models, change research decisions |
| Paper Server Health | `health` | Inspect deployed runtime and risk/observability evidence | modify code or champion selection |
| Forward Maker Research | `forward-shadow` | Collect isolated forward maker evidence | produce live intents, book hypothetical PnL, merge |
| Fast Arbitrage Shadow | `shadow-evidence` | Collect event-driven read-only arbitrage and cost-stressed shadow evidence | submit orders, mutate champion state, book real PnL |
| Arbitrage Theory Research | `research-cycle` | Re-derive candidate structures from valid shadow evidence and produce research-only proposals | approve itself, merge, deploy |
| External Intelligence | `live-api-smoke` | Every 30 minutes collect, timestamp, store and purged-walk-forward backtest free/public external information while reporting Gamma/CLOB/source health | treat a source as truth, write production signals, mutate champion, submit orders |
| Alpha Factory | `evaluate` | Compare paper-only challengers and attach fresh external evidence without self-promotion | mutate champion, deploy, execute |

The external worker is a strict replacement for the former connectivity-only six-hour live API smoke. It retains read-only Gamma/CLOB health checks, but now also creates durable point-in-time data and chronological evidence. Detailed contracts are in [`EXTERNAL_INTELLIGENCE.md`](EXTERNAL_INTELLIGENCE.md).

## Handoff graph

```text
research implementation / point-in-time external observations
        |
        v
Research Policy ---------> Research Queue
        |                         |
        | policy check            | evidence inventory
        v                         v
administrator research decision / labels
        |
        v
integration/* PR
        |
        v
Incumbent completion gate
(main == paper-validated == deployed HEAD; fresh health; recorder/broker alive)
        |
        v
Integration Merge -- repository_dispatch --> Post-Merge Validation
                                                |       |       |
                                                v       v       v
                                               CI   Monitoring  Live Paper
                                                                  |
                                                                  v
                                                         paper-validated
                                                                  |
                                                                  v
                                                            Deployment
                                                                  |
                                                                  v
                                                             Runtime Health
```

External information follows an additional research-only handoff before it may enter that graph:

```text
free/public source
  -> point-in-time normalization and provenance
  -> purged walk-forward backtest under 1x/1.5x/2x costs
  -> Alpha Factory visibility
  -> exact executable replay and incumbent ablation
  -> normal research approval
```

Administrator Supervisor observes every node and reports blockers; it mutates none of them.

## Incumbent completion gate

When private server deployment is enabled, the integration scheduler verifies:

```text
main
  == paper-validated
  == server head
  == server origin_main
  == server paper_validated
```

It also requires live recorder/broker processes and fresh health evidence. Missing, stale, future-dated or inconsistent evidence blocks the cycle. When private deployment is disabled, server evidence is skipped but `main == paper-validated` remains mandatory.

## Fail-closed sequencing

1. Unapproved work stays on `research/*`, `experiment/*` or `diagnostic/*`.
2. Evidence collection cannot modify production intents, PnL, sizing, exposure, drawdown, kill switches, OOS gates or authenticated execution.
3. External observations require separate source-event, retrieval and decision timestamps; ambiguous mappings abstain.
4. External backtests use only labels whose horizons elapsed before the next decision and report normal, 1.5x and 2x cost stress.
5. Approved reusable code is rebuilt on a fresh `integration/*` branch based on current `main`.
6. Integration is eligible only after required checks and all three labels are present.
7. Integration Merge performs only the squash merge and emits a handoff event.
8. Post-Merge Validation binds validation to the exact merged SHA.
9. Live-Paper Validation alone may advance `paper-validated`.
10. Deployment alone may install `paper-validated`.
11. Any failed, missing, stale or ambiguous gate leaves the preceding validated champion live.

## Why the split matters

The trading architecture separates probability estimation, executable trade decisions, portfolio/risk allocation and execution. The control plane mirrors that separation. Research evidence cannot become a trade merely because it compiles; an external probability cannot become alpha merely because it differs from Polymarket; integration cannot become production merely because it merges.

## Adding or replacing a scheduler

A scheduler workflow must:

1. have one top-level GitHub Actions job;
2. own a unique responsibility;
3. declare cadence, authority and forbidden actions in `config/scheduler_registry.json`;
4. preserve unique merge, validation-dispatch and deploy authorities;
5. include deterministic contract tests;
6. remain read-only unless its mutation is the narrowly documented responsibility.

`python3 scripts/validate_scheduler_registry.py` rejects unregistered workflows, multiple jobs or movement of privileged authority.
