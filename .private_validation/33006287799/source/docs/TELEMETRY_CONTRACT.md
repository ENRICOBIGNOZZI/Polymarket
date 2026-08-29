# Stable telemetry contract

Grafana must not be coupled to a trading-engine version. `monitoring/exporter_latest.py` auto-selects the highest `runs/paper_v*` runtime and exports stable `polymarket_runtime_*` metrics consumed by the default dashboard and alerts.

## Future-version rule

A future engine (V5, V6, ...) can change its internal files freely. To become immediately visible in the default Grafana dashboard it should publish this atomic JSON file in its run root:

```json
{
  "schema": "polymarket_runtime_status_v1",
  "equity": 10005.0,
  "pnl": 5.0,
  "drawdown": 0.002,
  "killed": false,
  "live_units": 1,
  "reserved_cash": 20.0,
  "gross_exposure": 40.0,
  "realized_pnl": 3.2,
  "execution_imbalance": 0.10,
  "execution_staleness": 4.0,
  "oos": {
    "trades": 35,
    "net_pnl": 12.0,
    "stressed_net_pnl": 6.0,
    "max_drawdown": 0.04,
    "bootstrap_pvalue": 0.03,
    "eligible_for_tiny_pilot": true,
    "production_threshold": 0.003
  }
}
```

Write `runtime_status.json` atomically (`tmp` + rename). Version-specific detailed exporters are optional; this canonical contract is sufficient for the stable home dashboard and risk alerts.

## Current compatibility

V4 predates the contract file, so `exporter_latest.py` derives the same canonical fields from `multileg_equity.csv`, `multileg_legs.csv`, `bundle_ledger.csv`, `trade_tape.csv`, and `walk_forward.json`. This fallback can remain for historical runs.

## Stable Prometheus namespace

The home dashboard and critical alerts use only `polymarket_runtime_*`, including equity, PnL, drawdown, kill switch, execution staleness/imbalance, realized paper PnL, OOS results and live-escalation eligibility.

Do not rename these metrics when the strategy engine version changes. Add new version-specific metrics separately when needed.
