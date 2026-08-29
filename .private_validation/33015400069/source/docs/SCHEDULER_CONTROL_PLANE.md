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
| Post-Merge Validation | `dispatch` | Dispatch CI, monitoring and live-paper validation for the exact merged SHA | merge, advance `paper-validated`, deploy |
| CI | `build-test` | Release/Debug build and deterministic tests | merge, deploy |
| Monitoring Validation | `validate` | Validate observability contracts | merge, deploy |
| Live-Paper Validation | `live-paper-smoke` | Validate public-data paper behavior and advance `paper-validated` on success | merge research, deploy arbitrary refs |
| Paper Server Deploy | `deploy` | Deploy only `paper-validated` and verify services | select models, change research decisions |
| Paper Server Health | `health` | Inspect deployed runtime and risk/observability evidence | modify code or champion selection |
| Forward Maker Research | `forward-shadow` | Collect isolated forward maker evidence | produce live intents, book hypothetical PnL, merge |
| Fast Arbitrage Shadow | `shadow-evidence` | Collect event-driven read-only arbitrage and cost-stressed shadow evidence | submit orders, mutate champion state, book real PnL |
| Arbitrage Theory Research | `research-cycle` | Re-derive candidate structures from valid shadow evidence and produce research-only proposals | approve itself, merge, deploy |
| Live API Smoke | `live-api-smoke` | Test read-only API connectivity | modify models or state |

## Handoff graph

```text
research implementation
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

Administrator Supervisor observes every node and reports blockers; it mutates none of them.
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
7. The post-merge scheduler binds validation to the exact merged SHA and dispatches CI, monitoring and live-paper validation.
8. Live-paper validation alone may advance `paper-validated` after successful evidence publication.
9. Deployment alone may install `paper-validated` on the private paper node.
10. Server health separately verifies deployed revision, processes, monitoring and risk telemetry.
11. Any failed, missing, stale or ambiguous gate leaves the preceding validated champion live.

## Why the split matters

The trading architecture already separates probability estimation, executable trade decisions, portfolio/risk allocation and execution. The control plane mirrors that separation. Research evidence cannot become a trade merely because it compiles; integration cannot become production merely because it merges; deployment cannot select a different model; runtime supervision cannot rewrite the system it monitors.

This design keeps the long-run objective intact: one powerful live champion containing complementary experts, with a single portfolio/risk layer and execution path, while each research and operational function evolves independently behind explicit interfaces.

## Adding a scheduler

A new workflow must:

1. have one top-level GitHub Actions job;
2. have a unique responsibility not already owned by another scheduler;
3. declare its cadence, authority and forbidden actions in `config/scheduler_registry.json`;
4. preserve the unique merge, validation-dispatch and deploy authorities;
5. include deterministic contract tests;
6. remain read-only unless its mutation is the narrowly documented responsibility.

`python3 scripts/validate_scheduler_registry.py` rejects unregistered workflows, multiple jobs, duplicate responsibilities with privileged authority, or movement of merge/deploy authority to the supervisor.
