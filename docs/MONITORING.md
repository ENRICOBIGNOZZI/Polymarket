# Grafana monitoring for the paper-live engine

This stack monitors the existing file-based paper runtime without adding code to the C++ trading path and without adding authenticated order submission.

## What is included

- `monitoring/exporter.py`: standard-library Python exporter that reads the restartable CSV/JSON runtime state and exposes Prometheus metrics on port `9108`.
- Prometheus: five-second scraping, 30-day default retention and local warning rules.
- Grafana: provisioned Prometheus datasource and the `Polymarket Paper-Live Monitor` dashboard.
- `docker-compose.monitoring.yml`: one-command local stack. All published ports bind to `127.0.0.1` by default.

The dashboard covers:

1. maker paper equity, PnL, return, drawdown, kill switch and state freshness;
2. cash, reserved cash, resting orders, open positions, fills, fees and traded notional;
3. pair and factor-neutral PCA statistical-arbitrage diagnostics;
4. NegRisk structural diagnostics, explicitly labelled **pre-gas/pre-latency**;
5. terminal-probability sleeve status and executable-edge signals, when that sleeve is running.

## Start

Build the C++ executables first, then start the paper loop:

```bash
cmake -S . -B build -DCMAKE_BUILD_TYPE=Release
cmake --build build -j
bash scripts/paper_v3_loop.sh config/paper_v3.json runs/paper_v3_live
```

In another terminal, start monitoring:

```bash
bash scripts/monitoring_up.sh
```

Open:

```text
http://localhost:3000
```

Default local login:

```text
user:     admin
password: polymarket-paper
```

Set a different password before startup for anything beyond a private local machine:

```bash
export GRAFANA_ADMIN_PASSWORD='replace-with-a-strong-local-password'
bash scripts/monitoring_up.sh
```

The optional environment variables are documented in `monitoring/.env.example`. `POLYMARKET_RUN_NAME` must match the final directory name passed as the second argument to `paper_v3_loop.sh`; its default is `paper_v3_live`.

## Endpoints

```text
Grafana:    http://127.0.0.1:3000
Prometheus: http://127.0.0.1:9090
Exporter:   http://127.0.0.1:9108/metrics
Health:     http://127.0.0.1:9108/healthz
```

Prometheus and the exporter are exposed only for local diagnostics. Remove their `ports` entries from `docker-compose.monitoring.yml` if host access is unnecessary; Grafana can still reach Prometheus over the internal Compose network.

## Runtime files consumed

The exporter reads, when present:

```text
runs/<run-name>/maker/maker_equity.csv
runs/<run-name>/maker/maker_order_log.csv
runs/<run-name>/maker/maker_fills.csv
runs/<run-name>/maker/maker_orders.csv
runs/<run-name>/maker/maker_positions.csv
runs/<run-name>/terminal/status.json
runs/<run-name>/terminal/signals.csv
runs/<run-name>/terminal/fills.csv
runs/<run-name>/terminal/broker_state.csv
runs/<run-name>/structural_latest.csv
runs/<run-name>/stat_arb_pairs.csv
runs/<run-name>/stat_arb_pca.csv
```

Long append-only fill and order logs are aggregated incrementally. The high-frequency maker equity file is tail-read, so the exporter does not rescan its full history every five seconds.

## Built-in warning rules

Prometheus evaluates four local rules:

- exporter unavailable for one minute;
- maker state older than 60 seconds for two minutes;
- maker drawdown at or above 10%;
- persisted maker kill switch active.

These rules are visible in Prometheus. No external Alertmanager or notification credentials are configured.

## Stop or reset

Stop containers while retaining Grafana and Prometheus history:

```bash
bash scripts/monitoring_down.sh
```

Remove the monitoring time series and Grafana local database as well:

```bash
docker compose -f docker-compose.monitoring.yml down -v
```

This does not delete the paper engine's `runs/` state.

## Validation

The exporter unit tests use only Python's standard library:

```bash
python3 -m unittest tests/test_monitoring_exporter.py -v
```

The CI job also parses the provisioned dashboard JSON and runs these tests.
