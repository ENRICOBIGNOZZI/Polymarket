# Phase 7 — declarative orchestration and fault isolation

Status: the canonical long-lived runtime inventory is manifest-bound and drift-failing.

`config/v7_process_manifest.json` declares all 33 long-lived processes: 31 launcher children, the PAPER launcher, and the exact-SHA runtime supervisor. Each process resolves to an executable and arguments, owner class, inputs and outputs, restart policy, liveness/freshness SLO, exact-SHA and config identity requirements, authority flags, dependencies, fault domain, drain behavior, and archival behavior.

`scripts/v7_process_manifest.py` validates 31/31 launcher-child parity, a 33-process total, an acyclic dependency graph, and exact long-lived authority counts. Promotion correctly has no long-lived owner because it remains an operator cutover action. The global coordinator is the sole long-lived coordinator/capital/risk/OMS/inventory owner; the ledger router is the sole ledger owner; the runtime supervisor is the sole runtime-identity owner.

Research processes resolve only to the isolated restart policy, zero authority, and `STOP_WITHOUT_INVENTORY` drain behavior. A canonical core process is forbidden from depending on a research fault domain, so a research collector cannot restart, stop, or mutate the economic core. Fault-injection tests cover authority injection, launcher/manifest drift, and core-to-research dependency injection.

The launcher consumes the manifest before spawning any child and writes the fully resolved contract to its exact-SHA control directory. Cutover requires both the manifest and validator. Monitoring publishes the manifest schema, version, process count, and SHA-256. Repeated PID registration moved into `scripts/v7_process_runtime.sh`, with an exact 31-child runtime assertion.

The repository contains no `legacy_stale_outputs` or `normalize_legacy_status` compatibility catalogue. Existing cutover tests continue to cover running, flat, never-started, zero-authority observer, partial-start, and stale/crashed incumbent drain states.

Gate evidence:

- `config/v7_process_manifest.json`
- `scripts/v7_process_manifest.py`
- `scripts/v7_process_runtime.sh`
- `tests/test_v7_process_manifest.py`
- `artifacts/v7_unification/runtime_process_graph.json`
- `scripts/paper_v7_execution_loop.sh`
