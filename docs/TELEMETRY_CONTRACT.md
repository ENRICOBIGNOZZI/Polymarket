# V7 Telemetry Contract

V7 is the only supported telemetry generation. The canonical monitoring plane is:

```text
monitoring/exporter_v7.py
  -> monitoring/prometheus_v7.yml
  -> monitoring/grafana/dashboards/polymarket-v7.json
```

Runtime telemetry is emitted from the canonical V7 run root and execution ledger. Monitoring must expose exact-SHA runtime identity, PAPER/authenticated-execution state, writer liveness, data freshness, fills, realized and marked PnL, inventory/exposure, drawdown, queue/fill diagnostics, adverse markout, fees/slippage/unwind costs, and market-maker reward/rebate attribution where applicable.

There is no compatibility exporter or automatic selection of another numerical runtime generation. Missing V7 telemetry fails closed.
