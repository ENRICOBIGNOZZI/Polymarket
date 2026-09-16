# Operator dashboard: telemetry boundaries

The existing `polymarket-v7` UID and Prometheus datasource remain canonical.
The exporter and dashboards are read-only. They never authorize execution,
change capital, write ledger events, or promote a research strategy.

Current tiles use instant queries with `last`, not `lastNotNull`. Every target
is scoped to the selected Prometheus instance. Displayed observations require
a usable exporter cache, a successful scrape and the relevant source's schema,
safety, runtime identity and age checks. The independent scrape connection,
snapshot age and Prometheus alerts remain visible during exporter failures.

`Verified net PnL` is unavailable until fresh canonical economics, the current
ledger and portfolio reconciliation agree. Guard equity change, canonical net
PnL and LEAD_LAG_TAKER_V1 forward-test PnL are separate measures, not additive.
A running runtime is not proof of accounting correctness or live-trading
permission. Registered crypto contexts are not proof of active execution.

Missing markouts and missing numeric observations are NaN, never synthetic
zeros. Completion is undefined without submissions. History is split by run
identity and never bridges missing observations. Validated history begins when
the new freshness metrics are first scraped; no historical data is modified.
Forward-test skip counters count repeated checks, not independent signals.

Rebuild the main dashboard with `python3 monitoring/build_operator_dashboard.py`.
Run `python3 -m pytest -q tests/test_monitoring_v7_*.py
 tests/test_v7_maker_lab_dashboard.py tests/test_v7_deploy_monitoring_manifest_contract.py`
as one shell command. Additional checks should parse every PromQL expression
against Prometheus and render the dashboard in a real browser.

A monitoring-only emergency release may run this exporter from a separate
immutable source directory, with `--repository-root` still pointing at the
canonical runtime checkout and `--run-root` at its existing PAPER run. Back up
the exporter LaunchAgent, Grafana provider and Grafana ini before changing
those paths. Restart only those two monitoring services. Do not invoke the
full runtime updater or change runtime SHA. Record and verify runtime PID/SHA
and PAPER safety flags before and after. The next normal deployment should
include this change and restore normal repository-relative monitoring paths.
