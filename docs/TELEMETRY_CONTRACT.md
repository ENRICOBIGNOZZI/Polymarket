# V7 telemetry contract

The operational telemetry contract is V7-only. `monitoring/exporter_v7.py` reads the canonical V7 runtime under `runs/paper_v7_live` and exposes the metrics consumed by Grafana, Prometheus alerts, deployment verification and server health.

## Canonical runtime state

The execution supervisor writes:

```text
runs/paper_v7_live/execution/runtime_status.json
```

with schema:

```json
{
  "schema": "polymarket_v7_runtime_status_v1",
  "timestamp": 0,
  "version": 7,
  "paper_only": true,
  "authenticated_execution": false,
  "starting_capital": 10000.0,
  "cash": 0.0,
  "equity": 0.0,
  "peak_equity": 0.0,
  "pnl": 0.0,
  "drawdown": 0.0,
  "killed": false,
  "live_units": 0,
  "reserved_cash": 0.0,
  "gross_exposure": 0.0,
  "realized_pnl": 0.0,
  "execution_imbalance": 0.0,
  "execution_staleness": 0.0,
  "strategies": {}
}
```

The file is written atomically. `version`, `paper_only`, `authenticated_execution` and the V7 schema are hard health invariants.

## Allocator and strategy state

The same execution root contains:

```text
allocator_status.json
strategy_status.csv
```

`allocator_status.json` uses schema `polymarket_v7_allocator_status_v1` and reports expected/alive strategy books, reserve fraction, gross limit/fraction and timestamp.

`strategy_status.csv` contains the five canonical V7 books:

```text
micro_maker
micro_taker
relative_value
hard_arb
external
```

with equity/PnL, positions, fills, liveness, staleness, drawdown and gross-exposure fields used by the V7 exporter.

## Market proxy state

```text
runs/paper_v7_live/execution/market_proxy_status.json
```

must use schema:

```text
polymarket_v7_market_proxy_status_v1
```

The public cache uses:

```text
polymarket_v7_market_proxy_cache_v1
```

No predecessor cache/status schema is accepted.

## Supervisor state

Outer runtime:

```text
runs/paper_v7_live/v7_supervisor.json
```

Execution child:

```text
runs/paper_v7_live/execution/v7_execution_supervisor.json
```

Health requires fresh timestamps plus live execution and shadow children.

## Prometheus namespace

Canonical runtime metrics:

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
```

Allocator and strategy metrics:

```text
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

V7 research/diagnostic metrics use the `polymarket_v7_*` namespace.

## No compatibility fallback

The exporter does not infer state from retired runtime layouts and does not auto-select a numerically highest run directory. A run name other than `paper_v7_live` is rejected by the V7 monitoring entrypoint.

Historical telemetry remains available through Git history or retained external evidence, not through runtime compatibility code in the current repository.
