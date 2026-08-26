# V7 PAPER execution and PnL

The canonical PAPER execution state is under:

```text
runs/paper_v7_live/execution/
```

V7 maintains independent strategy books for `micro_maker`, `micro_taker`, `relative_value`, `hard_arb` and `external`, with aggregate state produced by `scripts/v7_runtime_status.py`.

## Accounting

Canonical aggregate state:

```text
runtime_status.json
allocator_status.json
strategy_status.csv
```

Per-strategy state and ledgers remain in their corresponding execution directories. PnL is based on simulated executable fills and marked/liquidation values according to each strategy's execution contract, not on quote touches or theoretical scanner edge alone.

The runtime accounts, where applicable, for:

- authoritative market fees;
- executable depth and quantity-specific prices;
- slippage;
- queue state and fillability;
- submission/cancel latency;
- adverse markout;
- capital-time cost;
- partial fills and explicit unwind losses;
- joint multi-leg completion state.

For multi-leg strategies, a partial basket is not a completed arbitrage. Realized PnL includes the actual simulated entry/exit/unwind path recorded by the V7 broker/ledger.

## Observability

`monitoring/exporter_v7.py` exports aggregate and per-strategy PnL/equity/fill/exposure/health metrics. The canonical Grafana dashboard is `polymarket-multi-strategy-v7`.

The PAPER runtime is not evidence of real fills. It does not load wallet credentials or submit authenticated orders; authenticated execution remains disabled.
