# Polymarket Quant Engine

[![CI](https://github.com/ENRICOBIGNOZZI/Polymarket/actions/workflows/ci.yml/badge.svg)](https://github.com/ENRICOBIGNOZZI/Polymarket/actions/workflows/ci.yml)
[![Live API smoke](https://github.com/ENRICOBIGNOZZI/Polymarket/actions/workflows/live-smoke.yml/badge.svg)](https://github.com/ENRICOBIGNOZZI/Polymarket/actions/workflows/live-smoke.yml)

C++20 live-data **paper-trading and relative-value research engine** for Polymarket. The V3 design keeps economically different alphas separate instead of forcing every signal into one terminal-probability ensemble.

There is **no authenticated order submission code** in this repository. It cannot place real-money orders.

## Repository status

| Component | Current status |
|---|---|
| Authoritative code | `main`: V3 paper-only engine |
| Structural arbitrage | Implemented as read-only executable diagnostics |
| Pair and PCA statistical arbitrage | Implemented as costed relative-value scanners |
| Maker execution | Conservative queue-aware paper simulator |
| Real-money broker | Intentionally absent |
| Active broad development | Draft PR [#17](https://github.com/ENRICOBIGNOZZI/Polymarket/pull/17) for V4 research and execution realism |

Required CI is deterministic and runs both Release and Debug builds. Public Gamma/CLOB integration is checked by a separate scheduled or manually triggered read-only workflow, so external API instability does not invalidate the code test suite.

Branch roles, merge gates and experiment cleanup are defined in [`docs/DEVELOPMENT.md`](docs/DEVELOPMENT.md). `main` is the only authoritative line; unmerged branches are research-in-progress.

## V3 architecture

### Strategy A — structural arbitrage

Forecast-free constraints priced on the live CLOB:

- binary YES/NO parity diagnostics;
- complete non-augmented NegRisk buy-all-YES baskets;
- NegRisk `NO_i -> YES_{j != i}` conversion opportunities;
- displayed depth, taker fees and slippage are applied before an opportunity is called executable;
- conversion opportunities remain explicitly pre-gas/pre-latency until those costs are measured rather than guessed.

Executable: `polymarket_negrisk_arb`.

### Strategy B1 — pair statistical arbitrage

Medium-horizon relative value in timestamp-aligned logit probability space. Candidate pairs are fit across several windows, their residuals are screened for mean reversion, half-life and split-sample stability, and current dislocations are mapped into expected mark-to-market convergence for both legs.

The output is an **expected price move, not `P(YES)`**. It is therefore not passed to binary-outcome Kelly sizing. Both taker/taker and maker-entry/taker-exit economics are reported.

Executable: `polymarket_stat_arb`.

### Strategy B2 — PCA / factor-residual statistical arbitrage

A timestamp-aware sparse-panel factor model replaces the legacy index-aligned PCA interpretation:

- history is bucketed by actual timestamps;
- correlations use only common timestamp observations;
- factor scores use only markets observed at each timestamp;
- residual mean reversion is estimated only across consecutive time buckets;
- a candidate is traded conceptually as a **factor-neutral basket**, not as a directional single-market residual;
- target plus hedge legs must neutralize estimated factor exposure within tolerance;
- fees, spread/slippage and depth are charged on every leg.

Again, this produces expected convergence rather than a terminal outcome probability.

Executable: `polymarket_pca_stat_arb`.

### Conservative paper-maker execution

`polymarket_maker_paper` is an execution simulator, not another alpha model.

- a passive buy joins the displayed best bid and records `queue_ahead`;
- merely touching the limit is **not** a fill;
- a fill requires later strict trade-through evidence;
- stale quotes and TTL-expired orders are cancelled;
- entry is maker-priced with zero maker fee; exit is deliberately simulated as taker with book walk, slippage and fee;
- resting orders reserve cash and count toward event/gross/drawdown budgets;
- peak equity and the drawdown kill-switch persist across restarts.

This is deliberately harsher than an assumed fill-probability model.

### Terminal-probability sleeve

The original probability engine remains available for information that is genuinely about terminal resolution. `config/paper_v3.json` assigns zero terminal-ensemble weight to `micro` and the legacy PCA expert, so short-horizon price signals cannot contaminate terminal probabilities or Kelly sizing. Graph/external/semantic terminal information can still be evaluated there.

## Build

Ubuntu/Debian:

```bash
sudo apt-get update
sudo apt-get install -y build-essential cmake pkg-config libcurl4-openssl-dev libboost-all-dev python3
cmake -S . -B build -DCMAKE_BUILD_TYPE=Release
cmake --build build -j
ctest --test-dir build --output-on-failure
```

The build produces:

```text
polymarket_engine
polymarket_negrisk_arb
polymarket_stat_arb
polymarket_pca_stat_arb
polymarket_maker_paper
```

## Run the V3 paper diagnostics once

```bash
bash scripts/paper_v3_once.sh
```

This runs Strategy A, both Strategy B sleeves, one conservative maker tick, and a terminal-probability scan. It does not submit authenticated orders.

For a local multi-cadence paper process:

```bash
bash scripts/paper_v3_loop.sh
```

The default loop evaluates maker microstructure roughly every 10 seconds, structural arbitrage every 30 seconds, and refits the slower statistical-arbitrage sleeves every 15 minutes.

## Terminal one-shot scan

```bash
./build/polymarket_engine \
  --config config/paper_v3.json \
  --once --scan-only --markets 50
```

## Runtime state

The terminal engine persists signals, fills, market history, broker/risk state, expert scores and status snapshots under its configured `run_dir`.

The maker simulator persists separate order/position/risk state and logs:

- `maker_order_log.csv` — posts, fills and cancellations;
- `maker_fills.csv` — simulated maker entries and taker exits;
- `maker_equity.csv` — equity, reserved cash and drawdown state;
- `maker_orders.csv` / `maker_positions.csv` / `maker_risk.csv` — restartable simulator state.

Statistical-arbitrage scanners write independent CSV diagnostics so Strategy B1 and B2 can be evaluated separately from Strategy A and from execution.

## External terminal information

`data/external_signals.csv` accepts a terminal probability and confidence:

```text
market_key,q_yes,confidence,source,timestamp
123456,0.63,0.80,my_model,1787472000
```

`market_key` may be a Gamma market ID, condition ID, or slug. Confidence decays with age. This interface belongs to the terminal-probability sleeve, not to short-horizon stat-arb.

## Risk model

The original terminal paper engine applies executable price, taker fee, book impact, slippage and model uncertainty before fractional-Kelly sizing, then clips by trade/market/event/gross/cash/drawdown-loss limits.

The maker simulator uses separate conservative reservation accounting for resting orders and open positions. The configured 15% drawdown is an **operating constraint, not a mathematical guarantee** against gaps, stale data, resolution shocks, API failures or software defects.

## Tests and live verification

Required CI builds the complete project in Release and Debug mode and runs deterministic unit plus local mock end-to-end tests.

The separate `live-api-smoke` workflow runs every six hours and can also be triggered manually. It performs a small read-only Gamma/CLOB scan, records `status.json` and `signals.csv`, and uploads the diagnostics. It never creates paper fills or authenticated orders.

For the detailed V3 design rationale see [`docs/STRATEGIES_V3.md`](docs/STRATEGIES_V3.md). For repository workflow and safety gates see [`docs/DEVELOPMENT.md`](docs/DEVELOPMENT.md).

## V4: execution realism → paper-live → OOS → tiny pilot

The current V4 implementation deliberately skips a large external-information project and focuses on proving whether the existing structural/B1/B2 edges are executable. It adds a public trade-tape recorder, a persistent multi-leg maker paper broker with queue-ahead/partial-fill/cancel/unwind logic, an atomic B1/B2 intent pipeline, chronological walk-forward evaluation, execution-cost stress tests, and an opt-in tiny real-money pilot that is dry-run by default.

```bash
cmake -S . -B build -DCMAKE_BUILD_TYPE=Release
cmake --build build --parallel 2
ctest --test-dir build --output-on-failure
bash scripts/paper_v4_once.sh
# or, for continuous paper research:
bash scripts/paper_v4_loop.sh
```

Out-of-sample gate:

```bash
python3 scripts/walk_forward_v4.py \
  --ledger runs/paper_v4_live/bundle_ledger.csv \
  --output runs/paper_v4_live/walk_forward.json
```

The real-money adapter is **never run by the paper loop or CI**. See `docs/EXECUTION_V4.md` before using `scripts/tiny_live_pilot.py`.
