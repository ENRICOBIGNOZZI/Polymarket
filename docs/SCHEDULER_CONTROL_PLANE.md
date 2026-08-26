# V7 scheduler control plane

The machine-readable source of truth is `config/scheduler_registry.json`. Current operator authority is `config/operator_directives.json`.

The control plane manages one operational runtime: V7. A scheduler may perform only its registered bounded responsibility. Retired predecessor runtimes, version adapters and transitional validation lanes are not part of the control plane.

## Unique privileged authorities

Exactly one registered scheduler owns each privileged action:

- **Integration Merge** — merge authority;
- **Post-Merge Validation** — validation-dispatch authority;
- **Paper Server Deploy** — deployment authority.

No supervisor, research workflow, monitor or bridge may independently acquire those authorities.

## Main schedulers

| Scheduler | Job | Responsibility |
|---|---|---|
| Administrator Supervisor | `supervise` | Observe current directives, scheduler health and blockers |
| Research Policy | `enforce` | Enforce branch/provenance/directive/shadow-isolation policy |
| Research Director | `audit` | Allocate and inventory bounded V7 research |
| Promotion Controller | `decide` | Decide objective PAPER promotion eligibility from current evidence |
| Integration Merge | `merge` | Merge exactly one controller-authorized integration after exact-head/current-base revalidation |
| Control-Plane Event Bridge | `bridge` | Dispatch Promotion Controller after successful validation and Integration Merge after successful controller completion |
| Post-Merge Validation | `dispatch` | Dispatch exact-SHA CI, Monitoring and V7 live PAPER smoke |
| CI | `build-test` | Build/test and enforce V7-only repository contracts |
| Monitoring | `validate` | Validate V7 exporter/dashboard/runtime observability |
| V7 live PAPER smoke | `live-paper-smoke` | Validate exact V7 main SHA and fast-forward `paper-validated` only after success |
| Paper Server Deploy | `deploy` | Deploy only `paper-validated` |
| Paper Server Health | `health` | Read-only validation of deployed SHA, processes, execution state and monitoring |

Registered research schedulers collect evidence only. They cannot merge, deploy, mutate `paper-validated`, change the live champion or perform authenticated execution.

## Promotion and deployment graph

```text
research evidence
      |
      v
Promotion Controller
      |
      | ephemeral exact-head authorization
      v
Integration Merge
      |
      v
main
      |
      v
Post-Merge Validation
   /      |       \
  v       v        v
 CI   Monitoring   V7 live PAPER smoke
                       |
                       v
                paper-validated
                       |
                       v
              Paper Server Deploy
                       |
                       v
              Paper Server Health
```

`merged`, `validated`, `deployed` and `healthy` are distinct states.

## Automatic PAPER promotion

Manual approval is not required. Promotion Controller may authorize an integration only from current machine-readable evidence and current operator directives. For economically material changes the decision must remain bound to exact source code/provenance and relevant OOS, cost, statistical-stability, data-health and incremental-utility evidence.

Integration Merge does not independently decide that a change is good. It consumes the controller authorization, revalidates the current base/head and merges only the authorized integration.

## Exact-SHA validation

After merge, Post-Merge Validation dispatches:

```text
ci.yml
monitoring.yml
v7-live-paper-smoke.yml
```

with the exact merged SHA. The V7 live PAPER smoke may advance `paper-validated` only if:

- it validated that exact SHA;
- the SHA is current `main`;
- it has merged-PR provenance;
- the V7 runtime smoke succeeded.

The ref is fast-forwarded only; no force promotion is allowed.

## Deployment gate

Paper Server Deploy accepts only V7 and only `paper-validated`. Its verifier requires the deployed checkout to equal the validated ref and checks the canonical V7 runtime/monitoring state.

Paper Server Health is separate and read-only. It verifies the deployed revision, single-writer/process state, recorder/proxy health, runtime schemas, drawdown, Prometheus and Grafana.

## Event bridge

The bridge has no independent production authority. It may only:

- dispatch Promotion Controller after a successful validator completion or on its recovery cadence;
- dispatch Integration Merge after a successful Promotion Controller completion.

It does not contain a special-case research PR, pre-cutover evidence lane or predecessor-runtime route.

## Adding a scheduler

A new scheduled workflow must:

1. have exactly one top-level job;
2. have a unique registered ID/workflow/responsibility;
3. declare cadence and authority in `config/scheduler_registry.json`;
4. preserve the unique merge/validation-dispatch/deploy authorities;
5. have complete repository visibility before narrowing to its responsibility;
6. remain read-only unless mutation is its explicitly registered job;
7. not reintroduce retired runtime/version compatibility.

`python3 scripts/validate_scheduler_registry.py` enforces these constraints and rejects unregistered workflows or retired compatibility references.
