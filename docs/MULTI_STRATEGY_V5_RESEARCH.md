# Multi-Strategy V5 Research Decision

## Decision

Replace the terminal mixture-of-experts execution path with independent paper books for `micro`, `pca`, `graph`, `semantic`, and `external`. The strategies share public market data interfaces and operational infrastructure, but they do not average probabilities before trading.

The live-paper architecture becomes:

```text
public Polymarket + external data
        |
        v
independent model books
  micro | pca | graph | semantic | external
        |
        v
fixed capital allocator + global paper risk supervisor
        |
        v
aggregate and per-strategy telemetry
```

## Motivation

The incumbent V4 mixture can dilute a specialist signal with several market-anchored forecasts. It also obscures attribution: a single combined fair value does not reveal which model generated PnL, drawdown, turnover, or adverse selection.

V5 moves the combination point from probability space to capital space. Every strategy receives one active expert, an isolated state directory, a fixed initial capital fraction, local risk limits, and its own fills and PnL. A parent supervisor aggregates the books and enforces a persistent global paper drawdown kill switch.

## Initial allocation

The initial paper allocation is deliberately bounded:

| Strategy | Capital fraction |
|---|---:|
| Graph/logical consistency | 30% |
| PCA/statistical relative value | 20% |
| External information | 20% |
| Microstructure | 10% |
| Semantic relative value | 10% |
| Unallocated reserve | 10% |

The weighted child gross limits are constrained below the global gross limit. Capital is static in the first migration so performance attribution is not confounded by an adaptive allocator during the validation window.

## Admission policy

Each child uses exactly one expert weight equal to one and every other expert weight equal to zero. The parent V5 config itself sets all expert weights to zero, so accidentally launching the raw engine with the parent config fails closed and produces no terminal model evidence.

Uncertainty remains in each child admission rule, but expert disagreement disappears because a child contains one expert. Spread-derived uncertainty, executable prices, fees, slippage, Kelly sizing, market/event concentration, gross limits, and drawdown controls remain active.

## Risk and execution scope

- Paper execution only.
- No wallet credentials.
- No authenticated order submission.
- Persistent local book state per strategy.
- Persistent aggregate peak equity and global kill state.
- A 15% global paper drawdown kill switch.
- A 10% initial reserve.
- Child processes are restarted only while the global kill switch is inactive.

The existing recorder, maker, B1/B2 multileg diagnostics, structural arbitrage scanner, rewards diagnostics, fast shadow, and external-intelligence scheduler remain available. They are not mixed into the five independent fair-probability books.

## Observability contract

V5 publishes:

- `runtime_status.json`: canonical aggregate paper equity, PnL, drawdown, gross exposure, positions, kill state, and OOS fields;
- `allocator_status.json`: aggregate status plus all strategy snapshots;
- `strategy_status.csv`: one row per model with capital, cash, equity, PnL, drawdown, exposure, fills, staleness, and process health;
- `allocator_events.csv`: starts, restarts, failures, shutdowns, and global kills;
- isolated `signals.csv`, `fills.csv`, `broker_state.csv`, `risk_state.csv`, and `history.csv` under `strategies/<name>/`.

Grafana must display aggregate PnL and equity, PnL/equity/drawdown/exposure/fills for every model, model health, and the existing auxiliary execution sleeves.

## Validation gates

1. Config fractions plus reserve equal one.
2. Every generated child has exactly one active expert.
3. Protected connection, run-directory, capital, and expert-weight keys cannot be overridden by a strategy.
4. Weighted child gross caps do not exceed the global cap.
5. Aggregate equity equals reserve plus the sum of child equities.
6. The global kill state persists across restarts.
7. The latest-version exporter selects the V5 adapter and exports per-model metrics.
8. Grafana dashboard queries match exported metrics.
9. CI, monitoring, live-paper smoke, and deployment remain paper-only.

## Migration criterion

Promote V5 as the single live-paper champion only after the implementation PR passes Release and Debug builds, deterministic unit tests, monitoring validation, public-data smoke, and repository governance checks. The migration changes the paper champion, not the real-money execution boundary.
