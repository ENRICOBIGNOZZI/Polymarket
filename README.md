# Polymarket Quant Engine — V7

[![CI](https://github.com/ENRICOBIGNOZZI/Polymarket/actions/workflows/ci.yml/badge.svg)](https://github.com/ENRICOBIGNOZZI/Polymarket/actions/workflows/ci.yml)
[![Live API smoke](https://github.com/ENRICOBIGNOZZI/Polymarket/actions/workflows/live-smoke.yml/badge.svg)](https://github.com/ENRICOBIGNOZZI/Polymarket/actions/workflows/live-smoke.yml)

C++20/Python **PAPER-only** quantitative research and execution engine for Polymarket.

The repository has one operational runtime: **V7**. Retired predecessor configs, loops, monitoring adapters, dashboards, compatibility workflows and transition manifests are intentionally absent from the working tree; Git history is the archive.

## Canonical runtime

`config/live_champion.json` is authoritative and selects:

```text
version: 7
loop: scripts/paper_v7_loop.sh
config: config/paper_v7.json
run root: runs/paper_v7_live
validated deploy ref: paper-validated
```

V7 is PAPER-only and authenticated execution is disabled.

The runtime has one outer supervisor and one execution owner:

```text
scripts/paper_v7_loop.sh
├── execution/  -> scripts/paper_v7_execution_loop.sh
└── shadow/     -> scripts/v7_shadow_loop.py
```

`execution/` owns executable PAPER state and the canonical execution ledger. `shadow/` owns frequency-separated research state and cannot submit production intents or mutate execution/PnL state.

## V7 strategy books

The canonical execution status exposes five independent capital books:

- `micro_maker` — queue/depth/toxicity-aware maker execution with bounded cancel latency and depth-aware exits/markouts;
- `micro_taker` — causal executable round-trip taker model with entry/exit depth, authoritative fees, slippage, adverse markout and capital-time costs;
- `relative_value` — Graph/RV joint-state multi-leg execution with explicit partial-state unwind;
- `hard_arb` — structural arbitrage with strict receive/exchange freshness, sequential-leg revalidation and unwind accounting;
- `external` — external-information sleeve, fail-closed unless validated signals exist.

Separate shadow research covers PCA statistical arbitrage, Local Factor and cross-sectional ranking across approved horizons. PCA is single-leg residual statistical arbitrage; inference and PnL are not pooled across frequencies.

## Execution contract

For every order/basket, execution economics are measured from executable state rather than quote-touch assumptions. The system accounts for:

- displayed depth and queue state;
- submission/cancel latency;
- partial fills and cancel/replace;
- authoritative market fees;
- slippage and quantity-specific liquidation prices;
- adverse markout;
- capital-time cost;
- explicit unwind loss;
- joint multi-leg completion state rather than products of marginal fill probabilities.

A partially filled basket is not counted as a completed arbitrage.

## Build and test

Ubuntu/Debian:

```bash
sudo apt-get update
sudo apt-get install -y build-essential cmake pkg-config libcurl4-openssl-dev libboost-all-dev python3
cmake -S . -B build -DCMAKE_BUILD_TYPE=Release
cmake --build build -j
ctest --test-dir build --output-on-failure
```

Core binaries retained by V7 include:

```text
polymarket_engine
polymarket_negrisk_arb
polymarket_stat_arb
polymarket_pca_stat_arb
polymarket_maker_paper
polymarket_trade_recorder
polymarket_multileg_paper
polymarket_fast_arb_shadow
```

The V7 runtime itself is orchestrated by Python/shell workers under `scripts/v7_*` and `scripts/paper_v7_*`.

## Run V7 PAPER

```bash
bash scripts/paper_v7_loop.sh config/paper_v7.json runs/paper_v7_live
```

The execution process writes canonical state under:

```text
runs/paper_v7_live/execution/
```

including `runtime_status.json`, `allocator_status.json`, `strategy_status.csv`, `market_proxy_status.json`, the trade tape and strategy-specific state/ledgers.

The shadow scheduler writes research state under:

```text
runs/paper_v7_live/shadow/
```

## Validation and deployment

The production sequence is exact-SHA and fail-closed:

1. `ci` validates code and V7-only repository contracts.
2. `monitoring` validates V7 exporter/dashboard/runtime observability.
3. `V7 live PAPER smoke` validates the exact main SHA on public data.
4. A successful non-PR smoke may fast-forward `paper-validated` to that exact merged main SHA.
5. `deploy-paper-server` deploys only `paper-validated`.
6. `paper-server-health` verifies deployed SHA, process ownership, runtime/proxy state, metrics, Prometheus and Grafana.

`main`, `paper-validated` and the deployed checkout are never treated as interchangeable unless the explicit validation/deployment checks prove the relationship.

## Monitoring

Monitoring is V7-only:

```bash
bash scripts/monitoring_up.sh
```

The exporter entrypoint is:

```text
monitoring/exporter_latest_v7.py
```

and the canonical dashboard is:

```text
monitoring/grafana/dashboards/polymarket-multi-strategy.json
UID: polymarket-multi-strategy-v7
```

It shows aggregate PAPER PnL/equity/drawdown, per-strategy PnL/equity/exposure/fills/health, plus V7 shadow PCA, Local Factor, ranking and HF queue diagnostics.

## Risk and authority

Current operator authority is encoded in `config/operator_directives.json`. Important hard constraints include:

- PAPER-only runtime;
- authenticated execution disabled;
- 15% aggregate drawdown kill limit;
- one runtime owner and one execution ledger/broker authority;
- authoritative fees and executable depth required;
- fixed-dollar trade caps disabled in the current PAPER envelope, while percentage ceilings, Kelly, available cash, depth and risk/economic gates still bind.

Schedulers are registered in `config/scheduler_registry.json`; the validator rejects unregistered workflows, duplicate authority and reintroduction of retired runtime/version surfaces.

## Repository map

```text
config/      V7 runtime, research and governance manifests
scripts/     V7 execution/research workers and control-plane helpers
src/         C++ market-data, scanner and paper-execution primitives
monitoring/  V7 exporter, Prometheus alerts and Grafana dashboard
ops/         V7-only Linux/macOS bootstrap and deployment
research/    current V7 research evidence only
tests/       current runtime/research/governance contracts
```

See `docs/V7_UNIFIED_ENGINE.md`, `docs/EXECUTION_EVIDENCE_V7.md`, `docs/MONITORING.md`, `docs/SCHEDULER_CONTROL_PLANE.md` and `docs/SERVER_DEPLOY.md` for operational details.
