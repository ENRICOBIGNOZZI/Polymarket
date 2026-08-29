# Multi-Strategy V5

V5 replaces the terminal probability mixture with five independent paper books. `micro`, `pca`, `graph`, `semantic`, and `external` each run with one active expert, isolated capital and state, and separate PnL attribution. A parent allocator combines only capital and risk.

## Runtime

```text
public data and shared features
        |
        +-- micro book
        +-- PCA book
        +-- graph book
        +-- semantic book
        +-- external-information book
        |
        v
aggregate paper allocator
        |
        +-- canonical total PnL/equity
        +-- global 15% drawdown kill
        +-- per-model process supervision
        +-- Grafana total and per-strategy panels
```

The V5 parent configuration has all expert weights set to zero. The allocator materializes child configs in `runs/paper_v5_live/generated_configs/`; every child config has exactly one expert weight equal to one. Launching the parent config directly therefore fails closed rather than recreating a mixture.

## Capital allocation

- graph: 30%
- PCA: 20%
- external information: 20%
- microstructure: 10%
- semantic relative value: 10%
- reserve: 10%

The initial allocator is static. Dynamic reallocation is intentionally deferred until each book has enough observations for shrinkage-stable expected-return and covariance estimates.

## Files

Aggregate state:

- `allocator_manifest.json`
- `allocator_state.json`
- `allocator_status.json`
- `runtime_status.json`
- `strategy_status.csv`
- `allocator_events.csv`

Per strategy:

- `strategies/<name>/status.json`
- `strategies/<name>/signals.csv`
- `strategies/<name>/fills.csv`
- `strategies/<name>/broker_state.csv`
- `strategies/<name>/risk_state.csv`
- `strategies/<name>/history.csv`
- `strategies/<name>/engine.log`

## Safety

V5 is paper-only. It contains no authenticated execution, wallet, signing, funding, cancel/replace, or balance-reconciliation path. Existing executable-price, fee, slippage, Kelly, market/event concentration, gross-exposure, and local drawdown checks remain active inside every child. The parent adds a persistent global drawdown kill switch and stops restarting child books after it triggers.

## Grafana

The `Polymarket Multi-Strategy Paper` dashboard shows:

- aggregate paper PnL and equity;
- global drawdown and kill state;
- process count alive versus expected;
- PnL, equity, gross exposure, drawdown, positions, fills, and staleness by model;
- existing maker, multileg, structural, and OOS diagnostics through the inherited V4 exporter.

## Operations

The approved champion manifest selects:

- loop: `scripts/paper_v5_loop.sh`
- config: `config/paper_v5.json`
- run root: `runs/paper_v5_live`
- deployment ref: `paper-validated`

`paper_latest_loop.sh` remains the only runtime selector. Post-merge validation advances `paper-validated` only after the exact main SHA passes CI, monitoring, and live-paper smoke; the existing deploy workflow then updates `~/polymarket` on the paper server.
