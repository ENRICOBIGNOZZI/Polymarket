# Polymarket V7 scheduler control plane

Each registered GitHub Actions workflow owns one bounded responsibility and exactly one top-level job. The machine-readable source of truth is `config/scheduler_registry.json`.

The control plane is PAPER-only. Authenticated execution is disabled, and no workflow may create that authority for itself.

## Authorities

Three privileged actions are deliberately unique:

- `integration-merge` is the only merge authority;
- `post-merge-validation` is the only validation-dispatch authority;
- `paper-server-deploy` is the only deployment authority.

The Promotion Controller decides eligibility but does not merge. Server Health observes but does not mutate code or policy. Administrator Supervisor reports but has no production mutation authority.

## Registered schedulers

| Scheduler | Responsibility |
|---|---|
| Administrator Supervisor | Observe control-plane state and blockers |
| Research Policy | Enforce branch, provenance, operator authority and isolation |
| Research Queue | Inventory V7 research/evidence and route bounded work |
| Promotion Controller | Evaluate objective promotion gates and issue one ephemeral authorization |
| Integration Merge | Revalidate and merge one authorized integration |
| Control-Plane Event Bridge | Dispatch existing controller/merge workflows after successful prerequisite events |
| Post-Merge Validation | Dispatch exact-SHA CI, monitoring and V7 PAPER validation |
| CI | Build/test canonical V7 and generic infrastructure |
| Monitoring Validation | Validate V7 exporter/dashboard/runtime telemetry |
| V7 Live-Paper Validation | Validate public-data V7 PAPER behavior and advance `paper-validated` |
| Paper Server Deploy | Deploy exact `paper-validated` V7 |
| Paper Server Health | Verify deployed revision, ownership, data, risk and observability |
| Forward Maker Research | Collect prospective maker queue/fill/markout evidence |
| Alpha Factory | Rank V7 challengers by executable OOS economics |
| Meta-Supervisor | Coordinate bounded research/remediation dispatches |
| Fast Arbitrage Shadow | Collect strict freshness/depth/fee/legging evidence |
| Arbitrage Theory Research | Derive structural/Graph candidates from valid evidence |
| External Intelligence | Collect and chronologically validate public external information |
| Live API Smoke | Read-only Gamma/CLOB connectivity |
| V7 Point-in-Time Universe Archive | Archive immutable validated V7 market universes |

Non-scheduled validation/access workflows are explicitly allowlisted by the registry validator and cannot acquire merge/deploy authority.

## Handoff graph

```text
research / evidence
        |
        v
Research Policy + objective evidence
        |
        v
integration/* candidate
        |
        v
Promotion Controller
        |
        | autonomous-promotion-approved (ephemeral)
        v
Integration Merge
        |
        v
Post-Merge Validation
        |--------------------|----------------------|
        v                    v                      v
       CI               Monitoring        V7 Live-Paper Validation
                                                    |
                                                    v
                                            paper-validated
                                                    |
                                                    v
                                                Deploy
                                                    |
                                                    v
                                             Server Health
```

The Control-Plane Event Bridge can dispatch the existing Promotion Controller after validator completion and the existing Integration Merge after a successful controller completion. It cannot itself decide, merge, deploy, move `paper-validated` or submit orders.

## Promotion Controller

The controller evaluates open non-draft `integration/*` PRs against current `main`. It requires exact source research provenance and re-fetches trusted source comments/reviews so an untrusted PR body cannot spoof a governance verdict.

For economic changes, the controller additionally validates machine-readable promotion evidence, including configured OOS trade/economic/statistical/data-health gates and exact source-content matching. The selected PR receives `autonomous-promotion-approved` only for the current cycle; stale authorizations are removed and recomputed.

## Integration Merge

The merge workflow does not trust the earlier controller decision blindly. Immediately before merge it re-fetches the candidate/source, requires the current authorization label, checks that the base and head have not moved, repeats promotion/source-content checks and merges exactly the expected head.

## Exact-SHA validation

Post-Merge Validation binds all downstream validators to one merged `main` SHA. If `main` advances, the older dispatch is superseded rather than being mistaken for current evidence.

V7 live-paper validation advances `paper-validated` only when the validated SHA is exactly current `main` and has merged-PR provenance.

## Deployment convergence

Deployment consumes `paper-validated` and server health verifies the same revision. The operational target is:

```text
main == paper-validated == deployed HEAD
```

with fresh V7 runtime/monitoring health evidence. Merge success alone is not enough.

## Adding or modifying a scheduler

A managed workflow must:

1. contain exactly one top-level job;
2. be registered with a unique ID and workflow path;
3. declare cadence, responsibility and authority flags;
4. preserve unique merge/validation-dispatch/deploy authority;
5. have deterministic contract coverage;
6. remain read-only unless mutation is its narrowly defined responsibility;
7. preserve PAPER-only/authenticated-disabled separation.

Validate with:

```bash
python3 scripts/validate_scheduler_registry.py \
  --root . \
  --registry config/scheduler_registry.json
```

Unregistered managed workflows, duplicate authority, stale workflow references or retired runtime surfaces fail closed.
