# V7 Multi-Crypto Lead/Lag — Task Manifest

| Milestone | State | Gate / current fact |
|---|---|---|
| M0 Audit and integrity | VERIFIED | Frozen BTC identity verified. Canonical ledger audit: 1,203 records, 401/401 terminal orders, zero open orders, exact FILL↔FINAL matching, empty spool; recovery/reconciliation tests pass. |
| M1 Core common | VERIFIED_SHADOW | Six-asset venue factory, typed settlement contexts, dynamic rules discovery and capability registry are integrated through a causal ContractState binding rules/token identity, OracleHub reference and PM BookHub; FeatureEngine consumes the gate. Zero authority. |
| M2 Data plane | VERIFIED_SHADOW | Multi-venue feeds, six-asset OracleHub, state-only PM BookHub/label hub, unified FeatureEngine and single zero-authority shadow supervisor are exact-SHA verified. Not deployed to canonical runtime. |
| M3 Execution/accounting | NOT_STARTED | Must retain one global coordinator/execution owner/ledger. |
| M4 Speed | IN_PROGRESS | Exact compact PM v2 replay measures L1 FAK/no-chase latency and size mechanics over 0–1000 ms and 5–500 shares. Economic value remains blocked until a calibrated signal/cost model exists. |
| M5 Research | IN_PROGRESS | Causal normalized-shock/derivatives/cross-crypto features, audited feature tape and exact compact PM repricing labels implemented. Current smoke has 1 independent time cluster, so calibration/OOS alpha remains INSUFFICIENT_EVIDENCE. |
| M6 Multi-crypto forward | BLOCKED | ETH/SOL remain SHADOW until protocol freeze and M0-M5 gates. |
| M7 Breadth | IN_PROGRESS | XRP/DOGE/BNB M5/M15 discovery/rules gates passed; activation remains SHADOW-only and still depends on shared PM-book/oracle/feature gates. |
| M8 Capacity/readiness | NOT_STARTED | PAPER/shadow only; no real-money promotion. |
| M9 London regional migration | BLOCKED_AWS_ACCESS | Build/benchmark eu-west-2a/b/c, then PAPER cutover with a fresh ledger generation. |

## Non-negotiable invariants

- Frozen BTC forward is not edited, restarted or silently redefined.
- `paper_only=true`.
- `authenticated_execution=false`.
- `real_order_submission=false`.
- `real_capital_at_risk=false`.
- No automatic promotion.
- No new lane receives entry authority by configuration default.
- One global risk/execution authority and one canonical ledger writer.
- Missing market/rules/feed/oracle information is `BLOCKED`/`UNKNOWN`, never zero or false evidence.

## M9 — London regional migration

- Provision three Ubuntu 24.04 compute-optimized EC2 instances in `eu-west-2a`, `eu-west-2b`, `eu-west-2c` once AWS access is explicitly available.
- Bootstrap the exact candidate SHA, C++/Python dependencies, systemd, monitoring and Tailscale.
- Tailscale is admin/SSH/monitoring only; it must never sit in the trading data path.
- Benchmark each AZ with the same SHA/load: Polymarket WS receive path, Binance/Coinbase WS receive path, Polymarket HTTPS connect/TLS/TTFB, p50/p95/p99/p99.9, failures and jitter.
- Choose the AZ from measured end-to-end evidence, not geography assumptions.
- Implement and test `scripts/v7_regional_shootout.py`; it now evaluates same-SHA regional/AZ probes fail-closed and never authorizes cutover.
- Copy only durable datasets, model artifacts, registries and required configurations. Do not clone the current live `runs/` state wholesale.
- London starts a new run and a new ledger generation. The Mac ledger is sealed, not reused.
- PAPER first: `paper_only=true`, `authenticated_execution=false`, `real_order_submission=false`.
- Cutover requires exactly one canonical writer: stop/seal old writer, then start London supervisor and validate feeds/models/PAPER accounting.
- Update GitHub Actions deployment host to the selected London Tailscale address only after London PAPER passes health and single-writer checks.
- Do not touch the current Mac runtime merely to prepare London.

### Current blocker

AWS provisioning is not available from the current environment. Everything before provisioning — benchmark harness, bootstrap/runbooks, exact-SHA deployment contracts and migration checks — can be built without AWS access.
