# Grafana monitoring for the latest Polymarket runtime

Grafana is deliberately version-agnostic. The monitoring stack auto-selects the highest `runs/paper_v*` runtime and exposes a stable `polymarket_runtime_*` namespace, so V5/V6/... can replace the engine without replacing the default dashboard.

## Architecture

- `monitoring/exporter.py`: legacy/base metrics.
- `monitoring/exporter_v4.py`: optional detailed V4 adapter.
- `monitoring/exporter_latest.py`: stable front door; auto-selects the latest versioned runtime and emits canonical metrics.
- `docs/TELEMETRY_CONTRACT.md`: the small `runtime_status.json` contract future versions should publish.
- Prometheus: five-second scraping, local risk rules.
- Grafana: `Polymarket — Latest Runtime` is the stable home dashboard.

If a future runtime publishes `runtime_status.json`, the default dashboard works immediately even if all internal execution files changed. Version-specific exporters/dashboards are optional detail, not a dependency.

## Start

Start whichever paper engine is current. For V4 today:

```bash
cmake -S . -B build -DCMAKE_BUILD_TYPE=Release
cmake --build build -j
bash scripts/paper_v4_loop.sh config/paper_v4.json runs/paper_v4_live
```

Then, independently:

```bash
bash scripts/monitoring_up.sh
```

Open `http://localhost:3000`. By default `POLYMARKET_RUN_NAME=auto`, which selects the numerically highest versioned `paper_v*` directory. Set `POLYMARKET_RUN_NAME` only when intentionally pinning an older/historical run.

## Stable home-dashboard metrics

The critical dashboard and alerts depend only on canonical metrics such as:

- `polymarket_runtime_info`
- `polymarket_runtime_equity_usd`
- `polymarket_runtime_pnl_usd`
- `polymarket_runtime_drawdown_ratio`
- `polymarket_runtime_kill_switch`
- `polymarket_runtime_reserved_cash_usd`
- `polymarket_runtime_gross_exposure_usd`
- `polymarket_runtime_realized_pnl_usd_total`
- `polymarket_runtime_execution_imbalance_ratio`
- `polymarket_runtime_execution_staleness_seconds`
- `polymarket_runtime_oos_trades`
- `polymarket_runtime_oos_net_pnl_usd`
- `polymarket_runtime_oos_stressed_net_pnl_usd`
- `polymarket_runtime_oos_bootstrap_pvalue`
- `polymarket_runtime_oos_eligible`

These names are the long-lived contract. New engine versions may add detailed metrics but should not rename the canonical namespace.

## V4 compatibility

V4 is supported without a canonical JSON file: the latest exporter derives the canonical state from `multileg_equity.csv`, `multileg_legs.csv`, `bundle_ledger.csv`, `trade_tape.csv` and `walk_forward.json`. Future engines should prefer atomic `runtime_status.json` publishing.

## Alerts

Critical/warning rules follow the auto-selected runtime rather than a version number. They cover exporter availability, execution staleness, drawdown, kill switch, execution imbalance and stale OOS evidence. Legacy single-market maker alerts remain available as secondary diagnostics.

## Security

Grafana, Prometheus and the exporter bind to localhost by default. Change the default Grafana password before exposing the interface beyond a private machine. No Alertmanager credentials are stored in the repository.

## Stop / reset

```bash
bash scripts/monitoring_down.sh
```

To remove Prometheus/Grafana history as well:

```bash
docker compose -f docker-compose.monitoring.yml down -v
```

This does not delete paper-run state under `runs/`.

## Validation

```bash
python3 -m unittest tests/test_monitoring_exporter.py tests/test_monitoring_v4_exporter.py tests/test_monitoring_latest_exporter.py -v
python3 -m py_compile monitoring/exporter.py monitoring/exporter_v4.py monitoring/exporter_latest.py
python3 -m json.tool monitoring/grafana/dashboards/polymarket-latest.json >/dev/null
bash -n scripts/monitoring_up.sh scripts/monitoring_down.sh
```

The same contract is checked in CI.
