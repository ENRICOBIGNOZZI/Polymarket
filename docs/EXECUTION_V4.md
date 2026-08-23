# V4 execution, paper-live validation, and tiny pilot

V4 follows the deliberately narrow path **1 → 2 → 3 → 5**. It does not add a news, polling, bookmaker, macro, or other external-alpha stack.

## 1. Event-time trade tape and multi-leg paper execution

`polymarket_trade_recorder` records public Polymarket taker trades from the Data API into `trade_tape.csv`. The current implementation is **REST polling**, not a WebSocket latency feed. Therefore receipt lag must not be presented as exchange-to-colocation latency.

B1 and B2 remain pure alpha scanners and write immutable diagnostic CSVs. `scripts/build_v4_intents.py` converts those diagnostics into the standardized maker-bundle schema consumed by `polymarket_multileg_paper`. `scripts/merge_v4_intents.py` then admits only fresh, complete, non-duplicated bundles. This separation lets statistical models evolve independently from broker lifecycle logic.

A maker buy can fill only after a compatible public taker SELL print reaches or trades through the passive limit. The model consumes displayed `queue_ahead` before assigning any fill to us.

The execution state machine includes submission latency and order arrival time; price-time queue ahead; true partial fills; cancel latency; cancel/replace with queue-priority reset; maximum replace count; bundle completion as the **minimum** leg fill fraction; execution deadline; conservative leg-risk mark; forced cancel and taker unwind of incomplete bundles; taker exits through displayed depth with slippage and protocol fees; adverse-selection marks after fills; and persistent cash, bundle, leg, queue, kill-switch and tape-cursor state.

A B1/B2 bundle is complete only if every leg reaches the configured completion threshold (95% by default). A one-leg fill is not booked as successful statistical arbitrage. Final `UNWOUND` bundles remain in `bundle_ledger.csv` and therefore count against the strategy in OOS evaluation.

## 2. Continuous paper-live process

```bash
cmake -S . -B build -DCMAKE_BUILD_TYPE=Release
cmake --build build --parallel 2
ctest --test-dir build --output-on-failure
bash scripts/paper_v4_once.sh
# or
bash scripts/paper_v4_loop.sh
```

The continuous loop keeps the public trade recorder and multi-leg paper broker alive, refits B1/B2 every 15 minutes, converts scanner CSVs into atomic execution intents, scans structural arbitrage independently, and keeps terminal probability isolated from relative-value signals. The existing single-market maker simulator remains a separate microstructure diagnostic.

## 3. Walk-forward / OOS gate

```bash
python3 scripts/walk_forward_v4.py \
  --ledger runs/paper_v4_live/bundle_ledger.csv \
  --output runs/paper_v4_live/walk_forward.json
```

Candidate edge thresholds are chosen only on calibration data after an embargo, then frozen on each test fold. Reported metrics include realized gross/net PnL, fees, slippage, trade-level Sharpe/Sortino-like statistics, hit rate, profit factor, turnover, drawdown, per-strategy results, 1.5x cost stress, circular block-bootstrap inference and fold stability.

The default tiny-pilot gate requires at least 30 selected OOS trades, positive normal and stressed PnL, drawdown <= 10%, profit factor >= 1.10, bootstrap p-value <= 10%, and a majority of active folds positive. These are research gates, not guarantees of future returns. Only after the OOS result is fixed is a `production_threshold` learned from the latest closed historical calibration slice for the next forward pilot.

## 5. Tiny real-money pilot

`scripts/tiny_live_pilot.py` is **dry-run by default**. A normal call evaluates the OOS gate, selects at most one current complete bundle, resolves current token IDs/books, enforces hard caps and prints the proposed orders without submitting them.

Hard source-code caps are at most **$10 total bundle notional**, **$5 per leg**, one bundle per invocation, post-only maker entry, at most 10% of displayed bid queue by default, 95% minimum cross-leg completion and a $5 maximum live incomplete-leg-risk proxy.

Real submission additionally requires explicit `--execute`, `POLYMARKET_LIVE_ENABLE=I_UNDERSTAND_REAL_MONEY`, and credentials from environment variables. The pilot currently uses the supported CLOB v2 Python client as an isolated execution adapter; it is never invoked by the paper loop, CI, Grafana or the hourly read-only edge monitor.

If an unwind cannot be confirmed, the process exits non-zero and records unresolved exposure instead of pretending the account is flat.

## Monitoring

Grafana is not tied to V4. `monitoring/exporter_latest.py` auto-selects the highest `paper_v*` runtime and exposes stable `polymarket_runtime_*` metrics. Future engines can change internal files while preserving the home dashboard by publishing the canonical `runtime_status.json` contract documented in `docs/TELEMETRY_CONTRACT.md`.
