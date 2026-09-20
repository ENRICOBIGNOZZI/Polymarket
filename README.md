# Polymarket V7 — Crypto Only

Canonical PAPER trading and research system for Polymarket crypto markets.

`main` has one economic engine: `CRYPTO_SETTLEMENT_ENGINE`. Historical non-crypto
strategies are not part of the runtime or repository surface; Git history and the
pre-cleanup archive branch are the rollback path.

## Safety

The checked-in runtime is PAPER-only:

- `paper_only = true`
- `authenticated_execution = false`
- `real_order_submission = false`
- one global 15% maximum-drawdown kill threshold
- one execution owner and one append-only canonical ledger writer

Removing old strategies does **not** reallocate their risk budget to crypto.
Unused account capacity remains reserve until explicitly changed.

## Architecture

```text
Binance / Coinbase / Deribit        Polymarket public market data
              \                         /
               \                       /
                -> causal crypto state <-
                         |
                 incremental features
                         |
              CRYPTO_SETTLEMENT_ENGINE
          /              |               \
 settlement fair   informed taker   professional maker
          \              |               /
           -> V7_NATIVE_CRYPTO_SETTLEMENT_ENGINE
                         |
             one allocator -> one risk owner
                         |
                 one OMS / inventory owner
                         |
                 PAPER execution / replay
                         |
             one append-only execution ledger
```

The engine can compare `MAKE`, `TAKE`, `CANCEL`, `WITHDRAW`, and `NOTHING` on a
single conservative account-wealth objective. Components never own capital,
risk, orders, inventory, or the ledger independently.

## Crypto universe

The runtime discovers only registered crypto contexts. It does **not** scan the
whole Polymarket universe.

Canonical mapping:

```text
config/v7_crypto_settlement_markets.json
        -> scripts/v7_crypto_universe.py
        -> runs/paper_v7_live/universe/
```

Registered contexts are BTC, ETH, SOL, XRP, DOGE, and BNB at the configured horizons.
Exact rolling slugs are queried around the active settlement window. Discovery
has zero execution authority.

## London hot path

The London runtime is deliberately narrow:

```text
external/public feed
      -> in-memory state
      -> incremental features
      -> frozen-model inference
      -> global coordinator / risk
      -> PAPER execution
      -> reconciliation + minimal telemetry
```

London keeps what is needed for live crypto collection and execution research:
books, public trades, external feeds, exact stage timestamps, order lifecycle,
fills, queue/fillability evidence, markouts, settlement outcomes, ledger and
replay identities.

Training, hyperparameter search, historical backtests, notebooks, plots and
retrospective research are not part of the trading hot path.

Published research results live under [`docs/research`](docs/research/). The
first recent-data PAPER backtest, including its PNG charts, frozen parameters
and reproducibility receipts, is in
[`docs/research/simple-backtest-2026-09-20`](docs/research/simple-backtest-2026-09-20/REPORT.md).

## Live crypto components

### Settlement fair / informed taker

Contract mapping, settlement rule, fee authority, causal external features and
model identity are fail-closed. A missing or immature model never gains new-risk
authority merely because a market is discovered.

Core surfaces:

```text
config/v7_crypto_settlement_engine.json
config/v7_crypto_settlement_markets.json
config/v7_crypto_settlement_model_registry.json
config/v7_external_fair.json
scripts/v7_crypto_settlement.py
scripts/v7_external_fair_research.py
```

### Professional maker

Maker research remains because execution quality can improve the crypto engine.
It retains causal public-flow replay, queue-aware fillability, markouts, latency,
rest/cancel behaviour and model evidence. It has no independent authority.

```text
config/v7_professional_market_maker.json
scripts/v7_market_maker_rewards.py
build/polymarket_v7_maker_fillability_observer
build/polymarket_v7_maker_markout_observer
```

Shared L2 / WebSocket primitives under `src/fast_arb.cpp`, `src/fast_ws.cpp` and
`include/pm/fast_arb.hpp` are retained only as reusable execution/data-plane
infrastructure. There is no separate arbitrage engine or runtime owner.

## Execution evidence

Quoted edge is not PnL. Crypto research preserves the evidence needed to improve
future execution:

- external lead/lag events and exact receive clocks;
- Polymarket L1/L2 state and public trades;
- signal -> feature -> decision -> submit -> ACK/fill timestamps;
- queue and fillability observations;
- fill-conditioned markouts and adverse selection;
- cancellations, partial fills and unwind evidence;
- fees, slippage, settlement and realized PnL;
- deterministic replay keys and exact code/model/config identities.

Canonical execution surfaces:

```text
scripts/v7_execution_ledger.py
scripts/v7_ledger_spool.py
scripts/v7_canonical_economics.py
scripts/v7_joint_execution_policy.py
scripts/v7_learned_execution_hardened.py
runs/paper_v7_live/ledger/execution.jsonl
```

## Data retention

Live crypto data is a research asset, not disposable telemetry. Unique causal
sources and the canonical ledger are preserved according to the checked-in
catalog and retention policy. High-volume raw detail can use the authorized
rolling window only after content-addressed evidence/tombstone rules are met.

```text
config/v7_evidence_catalog.json
config/v7_data_retention.json
scripts/v7_evidence_catalog.py
scripts/v7_windowed_evidence_retention.py
monitoring/v7_retention.py
```

## Capital, risk and ownership

```text
scripts/v7_capital_allocator.py
scripts/v7_portfolio_guard.py
src/v7_crypto_settlement_engine.cpp
config/v7_authority_registry.json
config/v7_strategy_registry.json
config/v7_live_model_scope.json
```

There is one allocator, one risk owner, one OMS, one inventory owner and one
append-only ledger writer. Component observation budgets are research capacity,
not separate trading accounts.

## Monitoring

Monitoring follows the crypto runtime only:

```text
monitoring/exporter_v7.py
monitoring/prometheus_v7.yml
monitoring/v7_alerts.yml
monitoring/grafana/dashboards/polymarket-v7.json
monitoring/grafana/dashboards/polymarket-v7-latency.json
```

The latency dashboard reports current maker/runtime stages. Deleted strategy
latency streams are not treated as live evidence.

## Deployment

```text
scripts/paper_v7_execution_loop.sh
config/v7_process_manifest.json
ops/update_server_v7.sh
.github/workflows/v7-deploy-paper-server.yml
.github/workflows/v7-paper-server-health.yml
```

Deployment is exact-SHA and fail-closed. The server must expose the crypto
universe, canonical ledger, portfolio guard, execution state and monitoring
surfaces before health is declared.

## Development rule

A file belongs in the canonical repository only if it supports at least one of:

1. crypto market/data collection;
2. crypto signal/model inference;
3. crypto execution or risk;
4. crypto settlement/reconciliation;
5. crypto monitoring/recovery;
6. replay/backtest/training/evidence that can improve the crypto system.

Everything else belongs in Git history, not the live repository.

## Multi-rate native boundary

The canonical executable is `polymarket_v7_crypto_settlement_engine`. Fast
Binance/Coinbase/Polymarket events never wait for the cold context publisher.
The existing lifecycle manager publishes exact-run, market-bound context; an
isolated native reader validates it and hands POD snapshots through a bounded
SPSC queue. Each field retains its own receive clock, source identity and expiry.
Publication does not make stale data fresh. Unavailable fields remain null.

External protective cancels and book-lineage risk-off are independent of slow
context and do not require a new Polymarket book tick. Existing risk limits,
PAPER-only mode and canonical ownership are unchanged.

The optional probability/EV candidate remains opt-in research. Its current
18-feature artifact does not consume the new slow fields: evidence explicitly
reports `model_used_mask=0`. No trained slow-prior residual model, automatic
promotion, production cutover, profit or network-latency improvement is claimed.
See `docs/v7_multirate_migration.md` for the migration and validation boundary.
