# Polymarket scheduler control plane

The project no longer assigns research policy, evidence review, integration, validation, deployment and runtime supervision to one monolithic scheduler. Each workflow owns one bounded responsibility and exactly one GitHub Actions job. The machine-readable source of truth is [`config/scheduler_registry.json`](../config/scheduler_registry.json).

## Administrator contract

The project administrator owns the evolution of the single live champion. Automation may collect evidence, enforce policy, merge a pre-approved integration and execute validation/deployment handoffs, but it may not create production authority for itself.

A model integration can be merged automatically only when all three labels are present:

- `approved-for-integration`: the reusable implementation passed the research gate;
- `single-model-reviewed`: the change preserves one orchestrator, allocator, risk state and broker;
- `administrator-approved`: the project administrator explicitly authorizes this exact integration.

Removing any label immediately removes automatic merge eligibility. No scheduler uses an administrative bypass.

## One scheduler, one responsibility

| Scheduler | Job | Responsibility | Explicitly forbidden |
|---|---|---|---|
| Administrator Supervisor | `supervise` | Observe champion, workflow health, PR queues and blockers | approve research, merge, dispatch validation, deploy |
| Research Policy | `enforce` | Enforce branch/label/manifest/shadow policy | select alpha, merge, deploy |
| Research Queue | `audit` | Inventory evidence and integration backlog | approve, merge, change live configuration |
| Integration Merge | `merge` | Verify the incumbent is fully deployed/healthy, then merge at most one fully green administrator-approved `integration/*` PR | evaluate research, run validation, deploy |
| Post-Merge Validation | `dispatch` | Reconcile `main` versus `paper-validated` and dispatch exact-SHA validation when needed | merge, advance `paper-validated`, deploy |
| CI | `build-test` | Release/Debug build and deterministic tests | merge, deploy |
| Monitoring Validation | `validate` | Validate observability contracts | merge, deploy |
| Live-Paper Validation | `live-paper-smoke` | Validate public-data paper behavior and advance `paper-validated` on success | merge research, deploy arbitrary refs |
| Paper Server Deploy | `deploy` | Deploy only `paper-validated` and verify services | select models, change research decisions |
| Paper Server Health | `health` | Inspect deployed runtime and risk/observability evidence | modify code or champion selection |
| Forward Maker Research | `forward-shadow` | Collect isolated forward maker evidence | produce live intents, book hypothetical PnL, merge |
| Alpha Factory | `evaluate` | Evaluate paper-only alpha challengers and publish durable evidence | mutate the champion, merge, deploy, submit orders |
| Meta-Supervisor | `coordinate` | Check scheduler freshness every five minutes and relaunch only allowlisted stale workers | merge, deploy, health mutation, champion mutation |
| Fast Arbitrage Shadow | `shadow-evidence` | Collect event-driven read-only arbitrage and cost-stressed shadow evidence | submit orders, mutate champion state, book real PnL |
| Arbitrage Theory Research | `research-cycle` | Re-derive candidate structures from valid shadow evidence and produce research-only proposals | approve itself, merge, deploy |
| Live API Smoke | `live-api-smoke` | Test read-only API connectivity | modify models or state |

**Alpha Factory and Meta-Supervisor are separate schedulers.** Alpha Factory studies candidate alpha. Meta-Supervisor studies the scheduler graph itself and performs bounded recovery dispatches. Neither may absorb the other's economic or operational responsibility.

## Periodic heartbeat contract

Every registered scheduler has an explicit cron timer; event triggers are additional accelerators, not substitutes for periodic execution. The current timers are:

| Scheduler | Interval |
|---|---:|
| Meta-Supervisor | 5 minutes |
| Post-Merge Validation | 10 minutes |
| Monitoring Validation | 15 minutes |
| Research Policy | 30 minutes |
| Paper Server Deploy | 30 minutes when enabled |
| Forward Maker Research | 30 minutes |
| Administrator Supervisor | 60 minutes |
| Integration Merge | 60 minutes |
| CI | 60 minutes |
| Live-Paper Validation | 60 minutes |
| Paper Server Health | 60 minutes |
| Alpha Factory | 60 minutes |
| Fast Arbitrage Shadow | 60 minutes |
| Arbitrage Theory Research | 60 minutes |
| Research Queue | 120 minutes |
| Live API Smoke | 360 minutes |

The registry also declares a maximum acceptable staleness for each scheduler. Every five minutes, `control-plane.yml` compares recent default-branch runs with the current `main` SHA. It may relaunch only workflows with `meta_dispatch=true`. It explicitly refuses to dispatch:

- `integration-merge.yml`;
- `deploy-paper-server.yml`;
- `server-health.yml`;
- itself.

Those privileged transitions retain their own timers and gates. A watchdog can recover missing research or validation work, but cannot create merge or deployment authority.

## Handoff graph

```text
research implementation
        |
        v
Research Policy ---------> Research Queue
        |                         |
        | policy check            | evidence inventory
        v                         v
Alpha Factory / specialist research evidence
        |
        v
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

Meta-Supervisor watches scheduler freshness and may relaunch bounded workers.
Administrator Supervisor observes project state and reports blockers; it mutates none of them.
```

## Incumbent completion gate

When private server deployment is enabled, the integration scheduler runs ten minutes after the hourly server-health schedule, downloads the artifact from the latest successful `paper-server-health` run and verifies all of the following before even selecting a candidate:

```text
main
  == paper-validated
  == server head
  == server origin_main
  == server paper_validated
```

It also requires `recorder_alive=1`, `broker_alive=1` and a server-health timestamp no older than 7,200 seconds. Missing, stale, future-dated or inconsistent health evidence blocks the cycle. A successful live-paper validation without a matching recent healthy deployment is therefore not enough to start the next champion change.

When private deployment is explicitly disabled, the server evidence requirement is skipped, but `main == paper-validated` remains mandatory.

## Fail-closed sequencing

1. Unapproved work stays on `research/*`, `experiment/*` or `diagnostic/*`.
2. Evidence collection cannot modify production intents, PnL, sizing, exposure, drawdown, kill switches, OOS gates or authenticated execution.
3. Approved reusable code is rebuilt on a fresh `integration/*` branch based on current `main`.
4. Integration is eligible only after all required PR checks and the three explicit labels are present.
5. The integration scheduler refuses to merge until the incumbent is fully complete: `main == paper-validated`, and, when deployment is enabled, a fresh successful server-health artifact reports the same deployed SHA with live recorder and broker.
6. The merge scheduler performs only the squash merge and emits a handoff event. It does not run or dispatch the validation stack itself.
7. The post-merge scheduler binds validation to the exact merged SHA and dispatches CI, monitoring and live-paper validation. Its ten-minute timer repairs a missed handoff only when `main` is ahead of `paper-validated`.
8. Live-paper validation alone may advance `paper-validated` after successful evidence publication.
9. Deployment alone may install `paper-validated` on the private paper node.
10. Server health separately verifies deployed revision, processes, monitoring and risk telemetry.
11. Any failed, missing, stale or ambiguous gate leaves the preceding validated champion live.

## Why the split matters

The trading architecture already separates probability estimation, executable trade decisions, portfolio/risk allocation and execution. The control plane mirrors that separation. Research evidence cannot become a trade merely because it compiles; integration cannot become production merely because it merges; deployment cannot select a different model; runtime supervision cannot rewrite the system it monitors.

This design keeps the long-run objective intact: one powerful live champion containing complementary experts, with a single portfolio/risk layer and execution path, while each research and operational function evolves independently behind explicit interfaces. This matches the universal architecture's separation of probability estimation, trade decision, portfolio construction and execution.

## Adding a scheduler

A new workflow must:

1. have one top-level GitHub Actions job;
2. have a unique responsibility not already owned by another scheduler;
3. declare its cron, maximum staleness, recovery eligibility, authority and forbidden actions in `config/scheduler_registry.json`;
4. preserve the unique merge, validation-dispatch, recovery-dispatch and deploy authorities;
5. include deterministic contract tests;
6. remain read-only unless its mutation is the narrowly documented responsibility.

`python3 scripts/validate_scheduler_registry.py` rejects unregistered workflows, multiple jobs, duplicate responsibilities with privileged authority, or movement of merge/deploy authority to the supervisor. `tests/test_scheduler_periodicity.py` rejects a registered scheduler without an explicit timer or with a duplicated recovery authority.
