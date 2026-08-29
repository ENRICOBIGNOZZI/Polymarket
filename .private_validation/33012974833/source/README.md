# Polymarket Quant Engine

[![CI](https://github.com/ENRICOBIGNOZZI/Polymarket/actions/workflows/ci.yml/badge.svg)](https://github.com/ENRICOBIGNOZZI/Polymarket/actions/workflows/ci.yml)
[![Live API smoke](https://github.com/ENRICOBIGNOZZI/Polymarket/actions/workflows/live-smoke.yml/badge.svg)](https://github.com/ENRICOBIGNOZZI/Polymarket/actions/workflows/live-smoke.yml)

C++20 live-data **paper-trading, structural-arbitrage and statistical-arbitrage research engine** for Polymarket.

The core runtime is paper/read-only. An isolated Python tiny-live pilot exists for eventual execution validation, but it is dry-run by default, hard-capped, excluded from CI/paper loops, and cannot submit anything without explicit OOS approval, `--execute`, a consent environment variable and credentials.

## Current architecture

| Component | Status |
|---|---|
| Structural / NegRisk scanner | Read-only, costed diagnostics |
| B1 pair stat-arb | Timestamp-aligned relative-value scanner |
| B2 PCA/factor stat-arb | Explicit factor-neutral basket scanner |
| Public trade tape | Implemented |
| Single-market maker paper simulator | Queue/taker-tape driven, partial-fill aware |
| Multi-leg paper broker | Partial fills, leg risk, cancel/replace, unwind, persistence |
| Walk-forward/OOS gate | Implemented with embargo, cost stress and block bootstrap |
| Live champion selector | Explicit `config/live_champion.json`; never highest-version-by-name |
| Grafana/Prometheus | Version-agnostic; auto-selects latest `paper_v*` runtime |
| Tiny real-money pilot | Opt-in, <= $10 total / <= $5 per leg; never automatic |
| External fundamental/news alpha | Intentionally deferred |

The design keeps economically different objects separate:

```text
Strategy A: algebraic / structural constraints
Strategy B1/B2: expected mark-to-market convergence
Terminal sleeve: terminal resolution probability
Execution: fill / queue / leg-risk realization
```

Short-horizon relative-value signals are never silently reinterpreted as `P(YES)` or passed to binary Kelly sizing.

## Single live champion and research integration

`main` contains one integrated paper champion. Specialized alpha experts may coexist inside it, but there is one model orchestrator, one live configuration, one portfolio/risk allocator and one execution path. `config/live_champion.json` explicitly selects the loop, config and run root; adding a numerically newer `paper_vN` implementation does not promote it.

Unapproved work stays on `research/*`, `experiment/*` or `diagnostic/*`. A strictly isolated shadow diagnostic may enter `main` only when tests prove that it cannot emit production intents, book PnL or change risk. Once research is approved, System Watch creates a fresh `integration/*` branch from current `main`, consolidates the reusable improvement into the incumbent, removes superseded paths and merges only after the complete champion passes its gates.

The hourly governance workflow reports the research/integration queue and may merge at most one non-draft, fully green `integration/*` PR carrying `approved-for-integration` and `single-model-reviewed`. Post-merge CI, monitoring and live-paper smoke are explicitly dispatched; the server remains on the previous `paper-validated` revision until the new revision passes. See [`docs/SYSTEM_WATCH.md`](docs/SYSTEM_WATCH.md).

## Strategy A — structural arbitrage

`polymarket_negrisk_arb` scans:

- binary YES/NO parity;
- complete non-augmented NegRisk buy-all-YES baskets;
- `NO_i -> YES_{j != i}` NegRisk conversion structures;
- displayed depth, taker fees and slippage.

On-chain conversion opportunities remain explicitly pre-gas/pre-latency until those costs are actually measured.

## Strategy B1 — pair statistical arbitrage

`polymarket_stat_arb` works in timestamp-aligned logit probability space and screens residual relationships for mean reversion, half-life, stability and current dislocation. It reports expected convergence plus taker/taker and maker-entry/taker-exit economics.

Timed sports markets inherit the current pre-game safety gate from `main`; in-play contamination is not admitted into the B1 universe.

## Strategy B2 — factor-neutral PCA statistical arbitrage

`polymarket_pca_stat_arb` builds a sparse timestamp-aware factor model. A signal is a **factor-neutral basket**, not a directional single-market residual. Every hedge leg is costed and the basket is rejected if factor-neutralization error is too large. Timed sports use the same pre-game safety gate as B1.

## Scanner → adapter → broker

B1/B2 remain pure alpha scanners and write diagnostic CSVs. They do not know about broker state.

`scripts/build_v4_intents.py` converts scanner diagnostics into a stable execution-intent schema; `scripts/merge_v4_intents.py` atomically admits only fresh, complete, non-duplicate bundles; `polymarket_multileg_paper` owns queue, fills and lifecycle state.

This separation is intentional: models can change without rewriting the broker, and the broker can become more realistic without contaminating model estimation.

## Execution realism

`polymarket_trade_recorder` records public taker prints. The paper brokers do **not** treat quote touches as fills.

The multi-leg broker models:

- displayed queue ahead;
- submission and cancel latency;
- partial fills;
- cancel/replace and queue-priority reset;
- minimum cross-leg completion;
- execution timeout;
- leg-risk aborts;
- forced taker unwind;
- depth, slippage and protocol fees;
- adverse-selection marks;
- persistent cash/equity/peak/drawdown/kill state.

A partially filled basket is not counted as a successful arbitrage.

## Build

Ubuntu/Debian:

```bash
sudo apt-get update
sudo apt-get install -y build-essential cmake pkg-config libcurl4-openssl-dev libboost-all-dev python3
cmake -S . -B build -DCMAKE_BUILD_TYPE=Release
cmake --build build -j
ctest --test-dir build --output-on-failure
```

Main executables:

```text
polymarket_engine
polymarket_negrisk_arb
polymarket_stat_arb
polymarket_pca_stat_arb
polymarket_maker_paper
polymarket_trade_recorder
polymarket_multileg_paper
```

## Paper-live

One full read-only/paper V4 cycle:

```bash
bash scripts/paper_v4_once.sh config/paper_v4.json runs/paper_v4
```

Continuous champion process:

```bash
bash scripts/paper_latest_loop.sh
```

`paper_latest_loop.sh` reads `config/live_champion.json`; it does not infer approval from the largest version number. The selected loop keeps the trade recorder and multi-leg broker alive, updates the single-market maker sleeve frequently, refits B1/B2 every 15 minutes, refreshes structural/terminal scans and writes a walk-forward report hourly.

## Realized paper PnL

The durable source is:

```text
runs/<run>/bundle_ledger.csv
```

For a finalized bundle:

```text
paper net PnL = realized exit proceeds - realized entry cash
```

with protocol fees and simulated execution slippage already reflected in the entry/exit accounting. Incomplete bundles that are forcibly unwound stay in the ledger and count against the strategy.

## Walk-forward / OOS

```bash
python3 scripts/walk_forward_v4.py \
  --ledger runs/paper_v4_live/bundle_ledger.csv \
  --output runs/paper_v4_live/walk_forward.json
```

Thresholds are selected only on calibration data after an embargo and then frozen on each test fold. The default gate requires enough OOS trades, positive normal and 1.5x-cost-stressed PnL, controlled drawdown, profit factor, bootstrap evidence and fold stability.

Zero eligible trades is a valid research result; thresholds are not lowered merely to force activity.

## Grafana / Prometheus

Monitoring is deliberately **not version-specific**.

```bash
bash scripts/monitoring_up.sh
```

`monitoring/exporter_latest.py` auto-selects the numerically highest `runs/paper_v*` runtime and exposes stable `polymarket_runtime_*` metrics. The home dashboard is `Polymarket — Latest Runtime`.

Future V5/V6/... engines can change internal files freely and immediately reuse the same dashboard by publishing the atomic `runtime_status.json` contract in [`docs/TELEMETRY_CONTRACT.md`](docs/TELEMETRY_CONTRACT.md). They become live only through an explicit champion-manifest promotion after approved integration.

## Tiny real-money pilot

Dry-run only:

```bash
python3 scripts/tiny_live_pilot.py \
  --report runs/paper_v4_live/walk_forward.json \
  --intents runs/paper_v4_live/intents.csv
```

Real submission is additionally gated by the OOS report, hard source-code caps, explicit `--execute`, explicit consent and environment-only credentials. It is intentionally a tiny execution-validation tool, not the production broker. See [`docs/EXECUTION_V4.md`](docs/EXECUTION_V4.md) and [`docs/LIVE_GATE_V4.md`](docs/LIVE_GATE_V4.md).

## Safety / current limitations

- Paper fills are evidence under the documented queue model, not proof a live order would have filled.
- REST-polled public trades are not a colocation-grade latency feed.
- On-chain NegRisk conversion is not automatically executed.
- The 15% drawdown setting is an operating kill constraint, not a mathematical guarantee against gaps, outages or software defects.
- Real capital should not be scaled until realized paper/OOS evidence survives cost stress and a tiny forward execution pilot.

Detailed documents: [`docs/SYSTEM_WATCH.md`](docs/SYSTEM_WATCH.md), [`docs/EXECUTION_V4.md`](docs/EXECUTION_V4.md), [`docs/OOS_V4.md`](docs/OOS_V4.md), [`docs/MONITORING.md`](docs/MONITORING.md), [`docs/HOTFIX_INPLAY_QUEUE.md`](docs/HOTFIX_INPLAY_QUEUE.md).
