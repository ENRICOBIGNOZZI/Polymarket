# V7 monitoring

Monitoring is part of the canonical V7 runtime contract. It does not guess the active generation from directory names and it never falls back to an older exporter.

## Canonical components

- `monitoring/exporter_v7.py` — V7 Prometheus exporter;
- `monitoring/grafana/dashboards/polymarket-v7.json` — canonical dashboard;
- `docs/TELEMETRY_CONTRACT.md` — runtime telemetry contract;
- `scripts/runtime_action_report.py` — strategy/action explanation;
- `scripts/v7_runtime_status.py` — canonical atomic V7 runtime status.

The exporter reads the manifest-selected V7 state and exposes stable `polymarket_runtime_*` and V7 strategy metrics. Missing or incompatible runtime state fails closed rather than selecting another generation.

## Start locally

Run the canonical PAPER engine:

```bash
bash scripts/run_paper.sh
```

Then start monitoring:

```bash
bash scripts/monitoring_up.sh
```

For local development Grafana is normally available at `http://localhost:3000`.

## Private server access

On the PAPER server, Grafana stays bound to `127.0.0.1:3000` and is exposed privately through the configured Tailscale Serve route. The canonical operator URL is:

```text
http://mamma-portfolio.tail1bae85.ts.net
```

The machine opening the dashboard must be on the same tailnet. Prometheus and the exporter remain loopback-only.

## Runtime health

The health chain checks the exact deployed V7 revision and the canonical runtime root. Important evidence includes:

- single runtime owner and process-group integrity;
- recorder, broker and market proxy liveness;
- PAPER-only / authenticated-execution-disabled state;
- cash, equity, realized/marked PnL and drawdown;
- per-strategy PnL and fills;
- queue/fill/markout evidence where applicable;
- execution staleness and data health;
- Grafana dashboard UID/content consistency with the deployed revision.

A zero-trade interval is not interpreted as success or failure by itself. The action report must expose the admission/fill/economic reasons for abstention.

## Canonical metric family

The stable top-level namespace includes metrics such as:

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

V7 may expose additional strategy/frequency labels without changing the meaning of the canonical top-level metrics.

## Alerts

Prometheus alerts cover exporter availability, runtime/data staleness, drawdown, kill switch, execution imbalance and missing critical evidence. Alerts are keyed to canonical V7 state rather than a version-discovery heuristic.

## Security

Grafana, Prometheus and the exporter do not contain wallet credentials or authenticated Polymarket execution keys. Remote dashboard access is separated from trading authentication; the canonical runtime has authenticated execution disabled.

## Stop/reset

```bash
bash scripts/monitoring_down.sh
```

To remove local Prometheus/Grafana history:

```bash
docker compose -f docker-compose.monitoring.yml down -v
```

This does not delete PAPER state under `runs/`.

## Validation

```bash
python3 -m unittest \
  tests/test_monitoring_v7_exporter.py \
  tests/test_grafana_v7_contract.py \
  tests/test_server_health_readonly.py \
  tests/test_runtime_action_report.py -v
python3 -m py_compile \
  monitoring/exporter_v7.py \
  scripts/v7_runtime_status.py \
  scripts/runtime_action_report.py
python3 -m json.tool monitoring/grafana/dashboards/polymarket-v7.json >/dev/null
bash -n scripts/monitoring_up.sh scripts/monitoring_down.sh
```

The same contracts are exercised by `.github/workflows/monitoring.yml` and the private `server-health` workflow.
