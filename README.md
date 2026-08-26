# Polymarket Quant Engine

[![CI](https://github.com/ENRICOBIGNOZZI/Polymarket/actions/workflows/ci.yml/badge.svg)](https://github.com/ENRICOBIGNOZZI/Polymarket/actions/workflows/ci.yml)
[![Live API smoke](https://github.com/ENRICOBIGNOZZI/Polymarket/actions/workflows/live-smoke.yml/badge.svg)](https://github.com/ENRICOBIGNOZZI/Polymarket/actions/workflows/live-smoke.yml)

Unified V7 research and PAPER-trading system for Polymarket. The repository has one canonical runtime generation, one live champion manifest, one execution ledger, one broker authority and one runtime owner. Authenticated real-money execution is disabled.

## Canonical runtime

`config/live_champion.json` is authoritative and selects:

```text
version: 7
loop: scripts/paper_v7_loop.sh
config: config/paper_v7.json
run_root: runs/paper_v7_live
```

The runtime topology is defined in `config/v7_model_architecture.json` and the current operator envelope in `config/operator_directives.json`.

The live PAPER runtime contains independent strategy sleeves but does not average them into a single synthetic expert score:

- **Micro Maker** — event-time queue/fill/toxicity model with cancel latency, depth-aware exits and bounded-delay markouts;
- **Micro Taker** — complete round-trip executable EV with entry/exit depth, authoritative fees, slippage, markout and capital-time costs;
- **Graph / Relative Value** — prospective joint completion-state economics with explicit partial-fill and unwind accounting;
- **Hard Arbitrage** — per-leg receive/exchange freshness, sequential revalidation, depth, fee, legging and unwind checks;
- **External Intelligence** — fail-closed unless an approved probability mapping is available;
- **PCA statistical arbitrage** — single-leg residual mean reversion evaluated separately at 30m, 1h, 2h and 6h;
- **Local Factor** — leave-targets-out factor construction with dependence-robust inference at separate horizons;
- **Cross-sectional ranking** — relative top-vs-bottom ranking with horizon-separated state, PnL and inference.

PCA, Local Factor and ranking are research/shadow components unless explicitly admitted through the V7 execution contracts. Fixed-horizon relative-value signals are not interpreted as terminal `P(YES)` probabilities.

## Execution model

The V7 execution layer is built around auditable realized economics rather than quote touches. Core evidence includes:

```text
snapshot -> submission -> queue ahead -> partial/full fill -> fee/slippage
         -> cancel/unwind -> executable markout -> realized PnL
```

Multi-leg opportunities use a joint completion-state model. A partially filled basket is not counted as a completed arbitrage, and unwind losses remain in the ledger.

The main execution components are:

```text
scripts/v7_market_proxy.py
scripts/v7_micro_maker_worker.py
scripts/v7_micro_taker_worker.py
scripts/v7_graph_roundtrip_guard.py
scripts/v7_hard_arb_guard.py
scripts/v7_multileg_broker_runner.py
scripts/v7_runtime_status.py
```

`polymarket_trade_recorder` provides the public trade tape. `scripts/runtime_singleton_launcher.py` and `scripts/runtime_plane_supervisor.py` enforce a single canonical runtime owner and process-group cleanup.

## PAPER risk envelope

The current operator contract is PAPER-only with authenticated execution disabled. Key limits are encoded in `config/operator_directives.json` and validated by CI:

- market universe: up to 1000;
- minimum liquidity: $2;
- strictly positive post-cost edge floor: 0.5 bp;
- uncertainty penalty floor: 0;
- fractional Kelly ceiling: 25%;
- fixed-dollar trade cap disabled;
- trade/market/event/gross hard percentage ceilings: 100%;
- max drawdown: 15%.

The 100% values are hard ceilings, not targets. Available sleeve cash, Kelly sizing, executable depth, verified fees, slippage, adverse markout, capital-time costs, state integrity and the drawdown kill switch still bind.

## Build and test

Ubuntu/Debian:

```bash
sudo apt-get update
sudo apt-get install -y build-essential cmake pkg-config libcurl4-openssl-dev libboost-all-dev python3
cmake -S . -B build -DCMAKE_BUILD_TYPE=Release
cmake --build build -j
ctest --test-dir build --output-on-failure
```

The maintained C++ executables are:

```text
polymarket_engine
polymarket_trade_recorder
polymarket_rewards_scan
polymarket_fast_arb_shadow
```

Strategy-specific V7 execution and statistical research live in the `scripts/v7_*` modules instead of parallel versioned executables.

## Run the canonical PAPER engine

```bash
bash scripts/run_paper.sh
```

`run_paper.sh` builds the maintained binaries, acquires the repository-wide runtime lock and starts the manifest-selected V7 engine through the runtime supervisor.

Direct V7 loop invocation for controlled PAPER testing:

```bash
bash scripts/paper_v7_loop.sh config/paper_v7.json runs/paper_v7_live
```

The canonical state root is:

```text
runs/paper_v7_live/
  execution/
  shadow/
```

## Monitoring

Start Prometheus/Grafana support locally with:

```bash
bash scripts/monitoring_up.sh
```

The maintained exporter is `monitoring/exporter_v7.py` and the maintained dashboard is `monitoring/grafana/dashboards/polymarket-v7.json`. Runtime health is published through the canonical V7 `runtime_status.json` contract and is checked against the exact deployed revision.

## Validation and deployment

The control plane separates decision, merge, validation and deployment authority:

```text
research -> promotion-controller -> integration-merge
         -> exact-SHA CI + monitoring + V7 PAPER validation
         -> paper-validated -> deploy -> server-health
```

Important workflows:

```text
.github/workflows/ci.yml
.github/workflows/monitoring.yml
.github/workflows/v7-live-paper-validation.yml
.github/workflows/post-merge-validation.yml
.github/workflows/deploy-paper-server.yml
.github/workflows/server-health.yml
```

`paper-validated` advances only after successful exact-SHA V7 PAPER validation. Deployment consumes that exact validated revision. Server health checks deployed SHA, runtime ownership, recorder/broker/proxy state and observability without rewriting strategy policy.

## Research discipline

Research branches stay isolated until objective evidence is available. Promotion evidence is bound to exact source code and uses prospective/OOS execution economics, cost stress, drawdown, stability, data health and incremental utility. Evidence is not pooled across incompatible revisions or forecast horizons.

Point-in-time market universes are archived by `.github/workflows/v7-point-in-time-universe-archive.yml` for causal PCA, Local Factor and ranking research.

## Safety and limitations

- PAPER fills are simulated execution evidence, not proof that a real order would have filled.
- Public market/trade data are not a colocated low-latency feed.
- Missing executable depth, fee, freshness or state evidence fails closed where required by the strategy contract.
- The 15% drawdown control is an operational kill constraint, not a guarantee against gaps, outages or software defects.
- Authenticated order submission and real-money execution are outside the canonical runtime and disabled.

Current operational documentation lives in `docs/V7_UNIFIED_ENGINE.md`, `docs/EXECUTION_EVIDENCE_V7.md`, `docs/MONITORING.md`, `docs/SCHEDULER_CONTROL_PLANE.md`, `docs/SERVER_DEPLOY.md` and `docs/TELEMETRY_CONTRACT.md`.
