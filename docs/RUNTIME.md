# V7 Runtime

Run `scripts/paper_v7_execution_loop.sh` only from an isolated exact-SHA checkout. The loop acquires the canonical runtime lock and starts bounded public-data, maker, execution-evidence and monitoring children.

Required safety state is `paper_only=true`, `authenticated_execution=false`, `real_order_submission=false`. Startup fails closed on registry, ownership, fee, risk, model-identity or executable mismatches.

The deployed runtime must publish source SHA, configuration hash, policy hash, model hash, run ID, ledger ID and server ID. Repository code never guesses missing deployed identity.

Development must use a separate worktree. Never modify the incumbent checkout in place and never start a second writer.
