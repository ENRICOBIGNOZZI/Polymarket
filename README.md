# Polymarket V7

Canonical V7 PAPER trading and research system for Polymarket.

There is one architecture and one supported runtime generation: **V7**. Older numerical generations are retired; Git history is the archive.

## Safety

V7 is PAPER-only in this repository:

- `paper_only = true`
- `authenticated_execution = false`
- `real_order_submission = false`
- global maximum drawdown kill threshold: 15%

No wallet, key or authenticated order-submission path belongs in the canonical runtime.

## Canonical runtime

```text
Public Polymarket market data
        |
        v
V7 strategy/model sleeves
        |
        v
Canonical intent/execution routing
        |
        v
scripts/paper_v7_execution_loop.sh
        |
        +--> one append-only execution ledger / writer
        +--> learned execution and empirical joint-state evidence
        +--> one account-level capital allocator
        +--> one global portfolio guard / kill switch
        +--> one Prometheus / Grafana V7 monitoring plane
```

Canonical surfaces:

```text
config/paper_v7.json
config/live_champion.json
runs/paper_v7_live/
runs/paper_v7_live/ledger/execution.jsonl
monitoring/exporter_v7.py
monitoring/prometheus_v7.yml
monitoring/grafana/dashboards/polymarket-v7.json
ops/update_server_v7.sh
```

## Economic sleeves

All sleeves share the same runtime, ledger, risk and deployment system.

### Professional market maker

Primary market-making sleeve. It uses action-specific `JOIN` / `IMPROVE` / `FADE` / one-sided / withdraw decisions, inventory-aware pricing, causal public-flow replay, queue-aware fillability, full-depth executable marks and canonical-ledger evidence. Trading PnL, maker rebates and liquidity rewards are separate quantities. Bounded PAPER exploration receives no promotion credit.

```text
config/v7_professional_market_maker.json
scripts/v7_market_maker_core.py
scripts/v7_market_maker_model.py
scripts/v7_market_maker_rewards.py
build/polymarket_v7_market_maker_runtime
scripts/v7_market_maker_status.py
```

### Fast structural arbitrage

Shared C++ WebSocket/L2 substrate with dual exchange/receive clocks, lineage invalidation, executable depth and strict freshness semantics.

```text
src/fast_arb.cpp
src/fast_ws.cpp
src/fast_runtime/
src/v7_fast_structural_runtime.cpp
include/pm/fast_arb.hpp
```

### Settlement fair and informed taker

PAPER authority is configured for settlement-aware maker repricing/cancel and
full-depth informed taking. Contract correctness remains fail-closed: exact
rules, outcome mapping, fee and same-oracle binding are mandatory. Missing
bindings quarantine only the affected contracts; economic maturity is reported
but does not add another PAPER gate. Runtime readiness remains false until the
worker and oracle are actually healthy.

```text
config/v7_external_fair.json
scripts/v7_same_oracle_adapter.py
src/v7_external_*.cpp
```

### Graph / relative value

Native V7 multi-leg relative-value execution. Queue affects fillability only; it never creates capital capacity. Economics use direct empirical joint states and explicit partial/unwind losses.

```text
scripts/v7_graph_rv.py
scripts/v7_graph_rv_executable_intents.py
scripts/v7_graph_cost_vector.py
```

### Hard Arb

Native V7 structural complete-set execution research with authoritative fees, full depth, sequential leg revalidation and partial unwind.

```text
scripts/v7_hard_arb_guard.py
```

### Micro Taker

Selective short-horizon executable round-trip sleeve with entry/exit depth, fee, slippage and adverse-markout accounting.

```text
scripts/v7_micro_taker_worker.py
scripts/v7_micro_taker_core.py
```

### Ranking / PCA / Local Factor

V7-native research sleeves. Horizons remain separated and these models do not own runtime state or deployment.

### Strategy-wide registry and research kernels

The complete 15-family economic registry is validated at canonical runtime
startup. It fixes each sleeve's frequency, authority, action set, independent
sample unit and strategy-specific execution model. OSINT, sports latency,
cross-platform, wallet intelligence and market-open are implemented as
fail-closed research/shadow kernels; they cannot submit orders or auto-promote.

```text
config/v7_strategy_registry.json
scripts/v7_strategy_governance.py
scripts/v7_structural_relations.py
scripts/v7_osint_engine.py
scripts/v7_osint_pipeline.py
scripts/v7_osint_collector.py
scripts/v7_osint_likelihood.py
config/v7_osint_sources.json
scripts/v7_sports_latency.py
scripts/v7_cross_platform.py
scripts/v7_wallet_intelligence.py
scripts/v7_wallet_dataset.py
scripts/v7_market_open.py
scripts/v7_market_open_pipeline.py
scripts/v7_market_open_collector.py
```

Their current authority is explicit in the registry. Code availability is not
treated as economic validation: promotion requires causal replay,
chronological OOS, forward shadow/PAPER observations, direct execution-state
evidence and positive robust net PnL under cost stress.

## Execution evidence

Quoted edge is not PnL. Canonical observations are bound to model SHA and execution identities. The economic rule accounts for completion probability, fill-conditioned alpha/spread capture, fees, slippage, adverse markout, partial/unwind loss, capital cost and latency cost.

For multi-leg execution, direct empirical joint completion/state evidence is canonical. Products or minima of marginal fill probabilities are not substitutes. Economic assessment stresses the same frozen observations rather than reselecting trades.

```text
scripts/v7_execution_ledger.py
scripts/v7_ledger_spool.py
scripts/v7_canonical_economics.py
scripts/v7_joint_execution_policy.py
scripts/v7_learned_execution_hardened.py
```

## Capital and risk

```text
scripts/v7_capital_allocator.py
scripts/v7_portfolio_guard.py
```

Sleeve allocations are capacity budgets, not independent copies of the account. Cash, gross exposure, market/event exposure, inventory, drawdown and kill state reconcile at account level.

## Public trade recorder

```text
build/polymarket_v7_trade_recorder
src/v7_trade_recorder.cpp
```

The recorder writes the causal public tape used by the V7 PAPER execution models.

## Build and test

```bash
sudo apt-get update
sudo apt-get install -y build-essential cmake pkg-config libcurl4-openssl-dev libboost-all-dev libssl-dev python3
bash scripts/verify_v7.sh
```

The active PAPER runtime is:

```bash
bash scripts/paper_v7_execution_loop.sh
```

For unattended operation use the exact-SHA supervisor and the supplied
systemd/launchd service templates under `ops/`.

## Validation and deployment

The retained automation is deliberately small:

```text
.github/workflows/ci.yml
.github/workflows/monitoring.yml
.github/workflows/private-runtime-single-writer-validation.yml
.github/workflows/v7-live-paper-validation.yml
.github/workflows/v7-deploy-paper-server.yml
.github/workflows/v7-paper-server-health.yml
.github/workflows/v7-cross-sectional-ranking-research.yml
.github/workflows/v7-point-in-time-universe-archive.yml
```

CI, monitoring and single-writer checks validate V7 directly. There is no promotion/research/scheduler control-plane layer between them and the V7 code.

## Repository invariant

Do not add alternate numerical runtimes, compatibility PAPER loops, duplicate maker engines, duplicate ledgers, duplicate state writers, generic deployment entrypoints or authenticated execution. `tests/test_v7_repository_shape.py` enforces the slim V7-only repository shape.

Canonical documentation: [architecture](docs/ARCHITECTURE.md), [runtime](docs/RUNTIME.md), [execution](docs/EXECUTION.md), [latency](docs/LATENCY.md), [models](docs/MODELS.md), [data](docs/DATA.md), [replay](docs/REPLAY.md), [deployment](docs/DEPLOYMENT.md), [monitoring](docs/MONITORING.md), [model governance](docs/MODEL_GOVERNANCE.md), and [research](docs/RESEARCH.md).
