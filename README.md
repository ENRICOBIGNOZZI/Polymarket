# Polymarket V7 Quant Engine

[![CI](https://github.com/ENRICOBIGNOZZI/Polymarket/actions/workflows/ci.yml/badge.svg)](https://github.com/ENRICOBIGNOZZI/Polymarket/actions/workflows/ci.yml)
[![Live API smoke](https://github.com/ENRICOBIGNOZZI/Polymarket/actions/workflows/live-smoke.yml/badge.svg)](https://github.com/ENRICOBIGNOZZI/Polymarket/actions/workflows/live-smoke.yml)

V7 is the sole supported runtime, research destination, monitoring plane and PAPER champion for this repository. Superseded generation-specific runtime surfaces are intentionally retired and must not be reintroduced.

The system remains PAPER-only: authenticated execution and real-order submission are disabled by the canonical champion manifest.

## Canonical V7 runtime

The authoritative runtime contract is `config/live_champion.json`:

- execution loop: `scripts/paper_v7_execution_loop.sh`
- runtime config: `config/paper_v7.json`
- run root: `runs/paper_v7_live`
- validated deployment ref: `paper-validated`
- monitoring exporter: `monitoring/exporter_v7.py`
- Grafana dashboard: `monitoring/grafana/dashboards/polymarket-v7.json`

There is one champion, one portfolio/risk authority and one execution-evidence path. Research modules may coexist inside V7, but no alternate generation may become a runtime fallback.

## V7 strategy stack

V7 concentrates the active research and PAPER execution surfaces around:

- professional single-market market making with inventory, queue, adverse-selection and reward economics;
- complete-set / structural market making and arbitrage constraints;
- graph relative-value search and executable-intent construction;
- cross-sectional ranking across multiple horizons;
- PCA/statistical-arbitrage research;
- local-factor relative-value research;
- tightly isolated micro-taker execution experiments;
- external-intelligence research inputs;
- unified capital allocation, portfolio guards and execution evidence.

All strategy families route through V7 governance and execution contracts rather than maintaining independent runtime generations.

## Execution evidence

The execution layer is designed around executable economics rather than quote-touch accounting. Evidence includes order lifecycle, queue state, partial/full fills, costs, adverse selection, unwind outcomes and realized PAPER PnL. Multi-leg decisions are evaluated jointly rather than by multiplying independent leg-fill assumptions.

Canonical execution/evidence documentation lives in [`docs/EXECUTION_EVIDENCE_V7.md`](docs/EXECUTION_EVIDENCE_V7.md).

## PAPER operation

Run the canonical continuous PAPER process with:

```bash
bash scripts/paper_v7_execution_loop.sh
```

The loop reads the V7 configuration and writes into the canonical V7 run root. Real-order submission is not part of this runtime.

## Monitoring

V7 monitoring is native and version-pinned:

```text
monitoring/exporter_v7.py
    -> Prometheus V7
    -> Grafana polymarket-v7
```

The server-health path verifies the deployed plane and exact revision independently of the execution loop.

## Build and tests

Ubuntu/Debian:

```bash
sudo apt-get update
sudo apt-get install -y build-essential cmake pkg-config libcurl4-openssl-dev libboost-all-dev python3
cmake -S . -B build -DCMAKE_BUILD_TYPE=Release
cmake --build build -j
ctest --test-dir build --output-on-failure
```

CI additionally validates the V7 control plane, scheduler registry, project context, execution-evidence contracts and the repository-wide single-generation invariant.

## Governance

`main` is the canonical source branch. `paper-validated` is the deployment validation ref. Changes must preserve exact-revision provenance through CI, PAPER evidence, validation and deployment.

The repository-wide invariant is explicit: tracked source, config, workflow, tests, docs and monitoring surfaces may describe V7, but may not preserve superseded generation-specific runtime names or compatibility entrypoints.

## Safety state

- PAPER-only champion.
- Authenticated execution disabled.
- Real-order submission disabled.
- Credentials and SSH private keys remain outside version control.
- Drawdown and risk gates are operational controls, not guarantees against gaps, outages or software defects.
- Real capital promotion requires separate executable/OOS evidence and an explicit operator decision.

Key references: [`docs/EXECUTION_EVIDENCE_V7.md`](docs/EXECUTION_EVIDENCE_V7.md), [`docs/PROMOTION_EVIDENCE_BINDING.md`](docs/PROMOTION_EVIDENCE_BINDING.md), [`docs/SCHEDULER_CONTROL_PLANE.md`](docs/SCHEDULER_CONTROL_PLANE.md), and [`docs/TELEMETRY_CONTRACT.md`](docs/TELEMETRY_CONTRACT.md).
