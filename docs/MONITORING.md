# Grafana monitoring for the latest Polymarket runtime

Grafana is deliberately version-agnostic. The monitoring stack auto-selects the highest `runs/paper_v*` runtime and exposes a stable `polymarket_runtime_*` namespace, so V5/V6/... can replace the engine without replacing the default dashboard.

## Architecture

- `monitoring/exporter.py`: legacy/base metrics.
- `monitoring/exporter_v4.py`: optional detailed V4 adapter.
- `monitoring/exporter_latest.py`: stable front door; auto-selects the latest versioned runtime and emits canonical metrics.
- `docs/TELEMETRY_CONTRACT.md`: the small `runtime_status.json` contract future versions should publish.
- Prometheus: five-second scraping, local risk rules.
- Grafana: `Polymarket — Latest Runtime` is the stable home dashboard.
- `scripts/runtime_action_report.py`: explains the candidate funnel, hedge filtering, broker actions and abstention reasons in JSON and Markdown.

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

On the server, `ops/apply_runtime_config_macos.sh` keeps Grafana bound to `127.0.0.1:3000` and publishes only a private tailnet route with Tailscale Serve. The operator URL is:

```text
http://100.104.183.109:3000
```

The machine opening the page must be connected to the same Tailscale tailnet. Grafana uses anonymous `Viewer` access: there is no login form and no editing/admin permission through this route. Prometheus and the exporter remain loopback-only.

For a purely local installation, open `http://localhost:3000`. By default `POLYMARKET_RUN_NAME=auto`, which selects the numerically highest versioned `paper_v*` directory. Set `POLYMARKET_RUN_NAME` only when intentionally pinning an older/historical run.

## Hourly operational explanation

The `paper-server-health` workflow runs at minute 23 of every hour. It now verifies Grafana from a separate GitHub runner over the tailnet, not only from `localhost` on the server. It also generates and publishes:

```text
runs/paper_v4_live/action_report.md
runs/paper_v4_live/action_report.json
```

The report states:

- what B1, raw B2, coherent B2, NegRisk, rewards and the external expert found;
- how many PCA baskets were rejected as economically incoherent;
- which bundles passed the executable-edge gate;
- whether the broker posted, waited, partially filled, closed, unwound or abstained;
- the concrete blocking reason, such as costs, queue/fillability, missing external signals, conversion risk or broker-admission gaps.

The workflow embeds the Markdown report in the GitHub Actions summary and uploads both formats as health evidence. A zero-trade hour is therefore no longer reported merely as “healthy”: it must include the reason for abstention.

## B2 hedge coherence telemetry

Global PCA remains available as a research diagnostic in:

```text
stat_arb_pca_raw.csv
```

Before any B2 row reaches the intent adapter, `scripts/filter_coherent_hedges.py` checks every hedge leg against public Gamma metadata. Only same-event or sufficiently strong semantic relations enter:

```text
stat_arb_pca.csv
```

Rejected cross-domain or metadata-unknown baskets are retained for diagnosis in:

```text
stat_arb_pca_rejected.csv
```

The filter and intent adapter are both fail-closed. A scanner, metadata or filter failure produces an empty execution-facing B2 set; it never reuses a stale CSV. Public live telemetry exposes `raw_rows`, `coherent_rows`, `rejected_rows` and the corresponding positive-edge counts.

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

Grafana, Prometheus and the exporter bind to localhost. Remote Grafana access is proxied only through Tailscale Serve and receives Viewer permissions. No Grafana admin password, wallet credential, Alertmanager credential or authenticated Polymarket execution key is stored in the repository.

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
python3 -m unittest \
  tests/test_monitoring_exporter.py \
  tests/test_monitoring_v4_exporter.py \
  tests/test_monitoring_latest_exporter.py \
  tests/test_runtime_action_report.py \
  tests/test_coherent_hedges.py -v
python3 -m py_compile \
  monitoring/exporter.py \
  monitoring/exporter_v4.py \
  monitoring/exporter_latest.py \
  scripts/runtime_action_report.py \
  scripts/filter_coherent_hedges.py
python3 -m json.tool monitoring/grafana/dashboards/polymarket-latest.json >/dev/null
bash -n scripts/monitoring_up.sh scripts/monitoring_down.sh scripts/paper_v4_loop.sh
```

The same contract is checked in CI and in the hourly private-server health workflow.
