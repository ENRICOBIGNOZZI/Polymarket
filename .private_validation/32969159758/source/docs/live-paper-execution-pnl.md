# Live paper execution and PnL

The V4 paper runtime runs three execution-validation sleeves with separate accounting: maker simulation, multi-leg hedge simulation, and a continuous cost-aware taker paper engine.

The taker engine uses live public order books and the existing `Engine::paper_trade` admission path. A simulated fill is accepted only when the signal still clears the configured net-edge threshold after walking executable book depth, protocol fees, slippage, and the model-uncertainty penalty. It publishes `terminal/status.json` and `terminal/fills.csv`; the V4 exporter exposes those values to the `Polymarket — Fast Paper PnL` Grafana dashboard.

B1/B2 intent refresh is kept inside the broker's intent-freshness window so a valid candidate set does not become stale before the next scheduled scan.

This is paper execution only. It does not load wallet credentials, submit authenticated orders, or activate a real-money broker adapter.
