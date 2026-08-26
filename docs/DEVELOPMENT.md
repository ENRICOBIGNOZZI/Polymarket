# V7 development workflow

`main` is the only authoritative integrated code line. The operational PAPER runtime is V7 and is selected explicitly by `config/live_champion.json`.

The control plane is defined by `config/scheduler_registry.json` and `config/operator_directives.json`. No development change may reintroduce a predecessor runtime, multi-version selector, compatibility adapter or second execution owner.

## Branch roles

- `main` — integrated V7 PAPER system.
- `feat/*`, `fix/*`, `improve/*`, `ops/*` — focused implementation/infrastructure work.
- `research/*` — unapproved model hypotheses/evidence.
- `experiment/*`, `diagnostic/*` — temporary measurement/fault-isolation work.
- `integration/*` — consolidation of an approved research change into current V7.
- disposable CI/tmp branches — delete after use.

A branch is not an archive. Git commits/PR history are the archive.

## Single-runtime invariant

Current production-quality PAPER code must preserve:

```text
config/live_champion.json -> V7
scripts/paper_v7_loop.sh -> one outer supervisor
scripts/paper_v7_execution_loop.sh -> one execution owner
runs/paper_v7_live/execution -> canonical executable state
runs/paper_v7_live/shadow -> isolated research state
```

There is no supported alternate complete runtime in the working tree.

## Research isolation

Unapproved research cannot change:

- production intents;
- booked PnL;
- sizing or exposure;
- drawdown/kill state;
- `paper-validated`;
- deployment;
- authenticated execution;
- `config/live_champion.json`.

Shadow instrumentation may enter `main` only when tests prove it remains isolated from executable state and booked PnL.

## Integration

An approved research result is integrated into current V7 rather than appended as another stack.

The integration must:

1. start from current `main`;
2. bind to exact research provenance/evidence;
3. port only the reviewed reusable change;
4. preserve one runtime owner, execution ledger, broker authority and risk state;
5. delete superseded code/config/tests/workflows instead of adding a compatibility path;
6. update V7 tests and observability;
7. remain PAPER-only with authenticated execution disabled.

Promotion Controller decides objective promotion eligibility from current directives and evidence. Integration Merge performs only the controller-authorized merge after current-base/exact-head revalidation.

## Merge gates

Applicable changes must satisfy:

- Release and Debug builds;
- deterministic tests;
- exact V7 config/runtime contracts;
- state-persistence/failure behavior tests where relevant;
- no credentials or generated runtime state in Git;
- executable economic accounting for strategy changes;
- chronological/statistical controls for research changes;
- one V7 runtime after integration;
- no retired runtime/version surfaces.

For execution/model changes, economic validation includes relevant spread, fee, slippage, depth, queue/fillability, latency, adverse markout, uncertainty, capital-time and unwind costs.

## Post-merge lifecycle

A merge is not a deployment.

```text
integration merge
    -> exact-SHA CI
    -> exact-SHA monitoring validation
    -> exact-SHA V7 live PAPER smoke
    -> paper-validated
    -> V7 deploy
    -> server health
```

The promotion cycle is complete only when the validated/deployed relationships are explicitly proven by the corresponding workflows.

## Scheduler development

Every scheduled workflow under `.github/workflows` must be registered in `config/scheduler_registry.json`, have one top-level job and preserve unique privileged authority:

- merge — `integration-merge` only;
- validation dispatch — `post-merge-validation` only;
- deploy — `paper-server-deploy` only.

Run:

```bash
python3 scripts/validate_scheduler_registry.py
python3 scripts/validate_project_context.py
```

CI also rejects known retired runtime/version surfaces.

## Monitoring changes

Monitoring is V7-only. Update together as needed:

```text
monitoring/exporter_v7.py
monitoring/exporter_latest_v7.py
monitoring/grafana/dashboards/polymarket-multi-strategy.json
monitoring/prometheus/alerts.yml
```

Do not add a version dispatcher or fallback for historical run layouts.

## Safety boundary

The repository is PAPER-only. `config/paper_v7.json` and current operator directives require authenticated execution to remain disabled. A research result, merge, build, PAPER fill or deployment never constitutes real-money authorization.
