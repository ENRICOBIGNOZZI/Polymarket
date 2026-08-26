# Development workflow

`main` is the only authoritative integrated code line. The repository contains one canonical PAPER runtime generation: V7. `config/live_champion.json`, `config/v7_model_architecture.json` and `config/operator_directives.json` define the runtime, architecture and current operator envelope.

## Branch roles

- `main` — integrated V7 PAPER system;
- `feat/*`, `fix/*`, `improve/*`, `ops/*` — focused implementation/infrastructure work;
- `research/*`, `experiment/*`, `diagnostic/*` — isolated evidence-producing work;
- `integration/*` — exact-source integration candidates evaluated by the automatic promotion controller;
- `operator/*` — direct operator-authority changes only, with the required explicit instruction marker;
- `ci/*`, `tmp/*`, `noop` — disposable infrastructure branches.

Short-lived branches should be deleted after merge/closure. Git history and PR discussions are the archive.

## V7-only invariant

The integrated repository must preserve:

- one live champion manifest;
- one runtime owner;
- one execution ledger;
- one broker authority;
- one V7 PAPER configuration;
- authenticated execution disabled;
- no compatibility fallback to a retired runtime generation.

New strategy research extends V7 interfaces; it does not add another complete live stack.

## Research placement

Unapproved model work stays on research/experiment/diagnostic branches. It cannot change `config/live_champion.json`, book production PnL, alter live sizing/risk or submit authenticated orders.

A `shadow-isolated` exception is limited to measurement code with deterministic proof that it cannot mutate production intents, execution, PnL, allocation, risk or credentials.

Positive research governance must bind an exact source SHA. Source drift invalidates the approval.

## Integration

An `integration/*` candidate must start from current `main` and identify its exact source:

```text
Source research PR/branch/commit: #<number> / <research-branch> / <40-char-sha>
```

The Promotion Controller evaluates the candidate automatically. For economic changes it requires exact-source code matching plus the configured machine-readable OOS, cost-stress, drawdown, statistical-stability, data-health and incremental-utility evidence.

`autonomous-promotion-approved` is ephemeral and is re-evaluated each controller cycle. Integration Merge repeats the relevant checks immediately before merging the expected head.

## Merge gates

Applicable changes must satisfy:

1. Release and Debug builds;
2. deterministic unit/integration/regression tests;
3. current scheduler/project-context validation;
4. PAPER-only/authenticated-disabled boundaries;
5. current V7 operator limits;
6. correct executable economics for model/execution changes;
7. exact source provenance for integration work;
8. monitoring and failure-mode coverage for operational changes;
9. no credentials or generated runtime state committed;
10. no duplicate live runtime/broker/state-writer path.

A model change is not made eligible by weakening economics merely to create trades.

## Post-merge lifecycle

A merge is only the first state transition:

```text
integration merge
 -> exact-SHA CI
 -> exact-SHA monitoring
 -> exact-SHA V7 PAPER validation
 -> paper-validated
 -> deployment
 -> server-health
```

Promotion is complete only after the intended SHA is validated, deployed and healthy. An older healthy deployment is not evidence for a newer `main` revision.

## Scheduler development

Every managed workflow under `.github/workflows` must be represented in `config/scheduler_registry.json` and contain exactly one top-level job. The registry preserves unique authority:

- only `integration-merge` may merge;
- only `post-merge-validation` may dispatch the post-merge validation bundle;
- only `paper-server-deploy` may deploy.

Run:

```bash
python3 scripts/validate_scheduler_registry.py \
  --root . \
  --registry config/scheduler_registry.json
```

before modifying the control plane.

## Local validation

```bash
cmake -S . -B build -DCMAKE_BUILD_TYPE=Release
cmake --build build -j
ctest --test-dir build --output-on-failure
python3 scripts/validate_scheduler_registry.py --root . --registry config/scheduler_registry.json
python3 scripts/validate_project_context.py --root .
```

## Security boundary

The canonical repository/runtime is PAPER-only. Authenticated real-money execution is disabled. Private keys, wallet/exchange credentials and deployment secrets remain outside version control.
