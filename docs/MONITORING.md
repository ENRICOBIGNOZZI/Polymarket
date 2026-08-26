# V7 monitoring

The monitoring stack is V7-only. It reads the canonical PAPER runtime under `runs/paper_v7_live`, exports Prometheus metrics through `monitoring/exporter_latest_v7.py`, and provisions one operational Grafana dashboard.

## Runtime layout

```text
runs/paper_v7_live/
├── v7_supervisor.json
├── execution/
│   ├── v7_execution_supervisor.json
│   ├── runtime_status.json
│   ├── allocator_status.json
│   ├── strategy_status.csv
│   ├── market_proxy_status.json
│   ├── trade_recorder.log
│   └── strategy state / ledgers
└── shadow/
    ├── pca_stat_arb.json
    ├── local_factor_30m.json
    ├── local_factor_60m.json
    ├── cross_sectional_rank.json
    ├── hf_frequency_probe.json
    └── scheduler_status.json
```

There is no version selector or predecessor adapter. A monitoring process configured for any run other than `paper_v7_live` is rejected.

## Start

Run the V7 PAPER engine:

```bash
bash scripts/paper_v7_loop.sh config/paper_v7.json runs/paper_v7_live
```

Start monitoring:

```bash
bash scripts/monitoring_up.sh
```

The exporter listens on `127.0.0.1:9108`, Prometheus on `127.0.0.1:9090`, and Grafana on `127.0.0.1:3000` before the private Tailscale route is applied.

On the paper server, `ops/apply_runtime_config_macos.sh` keeps Grafana bound to loopback and exposes it through Tailscale Serve. The canonical operator URL is:

```text
http://mamma-portfolio.tail1bae85.ts.net
```

The machine opening the page must be connected to the same Tailscale tailnet with Tailscale DNS enabled. The remote route has Viewer permissions only. Prometheus and the exporter remain loopback-only.

## Canonical dashboard

The single operational dashboard is:

```text
monitoring/grafana/dashboards/polymarket-multi-strategy.json
UID: polymarket-multi-strategy-v7
```

It shows:

- aggregate PAPER PnL, equity, realized PnL and drawdown;
- global kill state and strategy liveness;
- per-strategy PnL, equity, gross exposure, positions, fills, drawdown and staleness;
- PCA shadow selections;
- Local Factor 30m/60m selections and signals;
- cross-sectional ranking IC/spread/gates by horizon;
- HF queue-clearability diagnostics by cadence.

Every Grafana query is required by tests to correspond to a metric emitted by `monitoring/exporter_v7.py`.

## Core Prometheus metrics

The operational namespace includes:

```text
polymarket_runtime_info
polymarket_runtime_equity_usd
polymarket_runtime_pnl_usd
polymarket_runtime_drawdown_ratio
polymarket_runtime_kill_switch
polymarket_runtime_live_units
polymarket_runtime_reserved_cash_usd
polymarket_runtime_gross_exposure_usd
polymarket_runtime_realized_pnl_usd_total
polymarket_runtime_execution_imbalance_ratio
polymarket_runtime_execution_staleness_seconds
polymarket_allocator_state_present
polymarket_allocator_models_expected
polymarket_allocator_models_alive
polymarket_allocator_global_gross_fraction
polymarket_model_info
polymarket_model_pnl_usd
polymarket_model_equity_usd
polymarket_model_open_positions
polymarket_model_fills_total
polymarket_model_alive
polymarket_model_staleness_seconds
polymarket_model_kill_switch
polymarket_model_drawdown_ratio
polymarket_model_gross_exposure_usd
```

V7 shadow research adds `polymarket_v7_*` metrics for PCA, Local Factor, ranking and HF cadence diagnostics.

## Exporter health

`/healthz` fails closed if any of the following is true:

- the V7 execution or shadow child is not alive;
- the outer supervisor is stale;
- the execution supervisor is stale;
- `runtime_status.json` is not the V7 PAPER schema;
- authenticated execution is not explicitly disabled;
- market-proxy status is missing, invalid or stale;
- the recent trade-recorder tail contains only data-path failures.

A process being alive is therefore insufficient to classify the runtime as healthy.

## Alerts

`monitoring/prometheus/alerts.yml` contains only V7 operational alerts. They cover:

- exporter availability;
- aggregate drawdown warning and hard limit;
- global kill switch;
- execution staleness and imbalance;
- allocator state;
- missing or stale strategy books;
- strategy-local kill switches;
- aggregate gross-limit breach.

No predecessor or simulated-live alert group is retained.

## Server health

`.github/workflows/server-health.yml` is read-only. It verifies:

- deployed `HEAD == paper-validated`;
- V7 live-champion manifest;
- outer and execution supervisor freshness;
- runtime, allocator, strategy and market-proxy schemas;
- recorder data health;
- unrecovered state-integrity failures;
- exporter, Prometheus and Grafana health;
- launchd/systemd service state;
- autoupdate status is bound to `paper-validated` and the deployed SHA.

It reports failures; it does not rewrite model policy.

## Security

No Grafana admin password, SSH private key, wallet credential, Alertmanager secret or authenticated Polymarket execution key is stored in the repository. Authenticated execution remains disabled in `config/paper_v7.json` and `config/operator_directives.json`.

## Stop

```bash
bash scripts/monitoring_down.sh
```

To remove Prometheus/Grafana history as well:

```bash
docker compose -f docker-compose.monitoring.yml down -v
```

This does not delete V7 PAPER runtime state under `runs/`.

## Validation

```bash
python3 -m unittest \
  tests/test_monitoring_v7_exporter.py \
  tests/test_monitoring_hard_safety_contract.py \
  tests/test_grafana_multi_strategy_contract.py \
  tests/test_server_health_readonly.py -v

python3 -m py_compile \
  monitoring/exporter.py \
  monitoring/exporter_v7.py \
  monitoring/exporter_latest_v7.py

python3 -m json.tool monitoring/grafana/dashboards/polymarket-multi-strategy.json >/dev/null
bash -n scripts/monitoring_up.sh scripts/monitoring_down.sh scripts/paper_v7_loop.sh scripts/paper_v7_execution_loop.sh
```
