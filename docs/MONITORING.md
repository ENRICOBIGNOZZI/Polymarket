# Grafana monitoring for the V4 paper-live engine

This stack monitors the file-based paper runtime without adding latency or authenticated order submission to the C++ execution path.

## What is included

- `monitoring/exporter.py`: the existing V3-compatible metrics.
- `monitoring/exporter_v4.py`: a thin V4 extension for public trade-tape health, multi-leg execution, realized paper PnL and walk-forward/OOS gates.
- Prometheus: five-second scraping, 30-day default retention and local warning rules.
- Grafana: provisioned Prometheus datasource and `Polymarket V4 — Paper Live` as the default home dashboard.
- `docker-compose.monitoring.yml`: local stack, bound to `127.0.0.1` by default.

The V4 dashboard covers multi-leg marked equity/PnL, reserved cash, drawdown and kill switch; trade-recorder freshness; live bundle completion and fill imbalance; realized bundle PnL; OOS/stressed PnL and bootstrap gate; and the existing structural, pair and factor-neutral PCA diagnostics.

## Start

Build V4 and start the continuous paper loop:

```bash
cmake -S . -B build -DCMAKE_BUILD_TYPE=Release
cmake --build build -j
bash scripts/paper_v4_loop.sh config/paper_v4.json runs/paper_v4_live
```

In another terminal:

```bash
bash scripts/monitoring_up.sh
```

Open Grafana at `http://localhost:3000`. The local default login is `admin / polymarket-paper`; set `GRAFANA_ADMIN_PASSWORD` to a strong value before exposing Grafana beyond localhost.

`POLYMARKET_RUN_NAME` must match the final directory name supplied to `paper_v4_loop.sh`; the default is `paper_v4_live`.

## Runtime files consumed

V3-compatible files remain supported. V4 additionally reads:

```text
runs/<run-name>/trade_tape.csv
runs/<run-name>/multileg_equity.csv
runs/<run-name>/multileg_bundles.csv
runs/<run-name>/multileg_legs.csv
runs/<run-name>/bundle_ledger.csv
runs/<run-name>/walk_forward.json
```

`multileg_equity.csv` is tail-read. The bundle ledger is intentionally append-only so realized paper PnL cannot be rewritten by later model changes.

## Important V4 metrics

- `polymarket_trade_recorder_staleness_seconds`
- `polymarket_multileg_equity_usd`
- `polymarket_multileg_pnl_usd`
- `polymarket_multileg_drawdown_ratio`
- `polymarket_multileg_kill_switch`
- `polymarket_multileg_bundle_completion_ratio`
- `polymarket_multileg_bundle_fill_imbalance_ratio`
- `polymarket_multileg_realized_net_pnl_usd_total`
- `polymarket_oos_net_pnl_usd`
- `polymarket_oos_stressed_net_pnl_usd`
- `polymarket_oos_bootstrap_pvalue`
- `polymarket_oos_eligible_for_tiny_pilot`

## Built-in warning rules

Prometheus warns if the public trade tape or multi-leg state becomes stale, if multi-leg drawdown exceeds 10%, if the persisted 15% kill switch is active, or if a live bundle develops more than 50 percentage points of cross-leg fill imbalance. The existing maker alerts remain enabled.

No external Alertmanager credentials are configured.

## Stop / reset

```bash
bash scripts/monitoring_down.sh
```

To remove monitoring history too:

```bash
docker compose -f docker-compose.monitoring.yml down -v
```

This never deletes `runs/`.

## Validation

```bash
python3 -m unittest tests/test_monitoring_exporter.py tests/test_monitoring_v4_exporter.py -v
python3 -m py_compile monitoring/exporter.py monitoring/exporter_v4.py
python3 -m json.tool monitoring/grafana/dashboards/polymarket-v4-live.json >/dev/null
```

The same checks run in CI.
