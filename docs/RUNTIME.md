# V7 Runtime

Run `scripts/paper_v7_execution_loop.sh` only from an isolated exact-SHA checkout. The loop acquires the canonical runtime lock and starts bounded public-data, maker, execution-evidence and monitoring children.

Required safety state is `paper_only=true`, `authenticated_execution=false`, `real_order_submission=false`. Startup fails closed on registry, ownership, fee, risk, model-identity or executable mismatches.

The deployed runtime must publish source SHA, configuration hash, policy hash, model hash, run ID, ledger ID and server ID. It also publishes component readiness. Configured PAPER authority is not equivalent to live readiness: the status remains `CORE_RUNTIME_ONLY` and `p0_full_stack_ready=false` until the settlement worker and verified oracle are observed healthy. Repository code never guesses missing deployed identity.

`ops/v7_runtime_supervisor.py` supplies bounded restarts, persistent backoff,
duplicate-writer rejection and fail-closed reconciliation. The systemd and
launchd templates keep the runtime, exporter and retention jobs alive after
shell logout without creating another execution owner.

Development must use a separate worktree. Never modify the incumbent checkout in place and never start a second writer.
