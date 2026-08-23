# V4 execution, paper-live validation, and tiny pilot

V4 follows the deliberately narrow path **1 → 2 → 3 → 5**. It does not add a news, polling, bookmaker, macro, or other external-alpha stack.

## 1. Event-time trade tape and multi-leg paper execution

`polymarket_trade_recorder` records public Polymarket taker trades from the Data API into `trade_tape.csv`. The current implementation is **REST polling**, not a WebSocket latency feed. Therefore `received_ms - trade_timestamp` measures polling/receipt lag and must not be presented as exchange-to-colocation latency.

`polymarket_multileg_paper` consumes standardized maker bundle intents emitted by B1 and B2. A maker buy can fill only after a compatible public taker SELL print reaches or trades through the passive limit. The model consumes displayed `queue_ahead` before assigning any fill to us.

The execution state machine includes:

- submission latency and order arrival time;
- price-time queue ahead;
- true partial fills;
- cancel latency;
- cancel/replace with queue-priority reset;
- maximum replace count;
- bundle completion as the **minimum** leg fill fraction;
- explicit execution deadline;
- conservative leg-risk mark;
- forced cancel and taker unwind of incomplete bundles;
- taker exits using full displayed book depth, slippage, and protocol fees;
- conditional adverse-selection marks after maker fills;
- persistent cash, bundle, leg, queue, kill-switch, and tape-cursor state.

A B1/B2 bundle is complete only if every leg reaches the configured completion threshold (95% by default). A one-leg fill is not booked as successful statistical arbitrage.

The durable accounting source is `bundle_ledger.csv`. Final `UNWOUND` bundles remain in the ledger and therefore count against the strategy in out-of-sample evaluation.

## 2. Continuous paper-live process

Build first:

```bash
cmake -S . -B build -DCMAKE_BUILD_TYPE=Release
cmake --build build --parallel 2
ctest --test-dir build --output-on-failure
```

One complete read-only/paper cycle:

```bash
bash scripts/paper_v4_once.sh
```

Continuous research process:

```bash
bash scripts/paper_v4_loop.sh
```

The loop runs two long-lived components:

- public trade recorder (10-second polling by default);
- multi-leg paper broker (3-second state-machine ticks).

B1/B2 are refit every 15 minutes. The intent merger writes the broker input atomically and drops stale, incomplete, duplicate-leg, or sub-threshold bundles. Structural arbitrage and terminal probability are still scanned independently, preserving the separation between algebraic constraints, relative-value convergence, and terminal probability.

The existing single-market maker simulator remains a separate microstructure diagnostic; it is not used to pretend B1/B2 atomic execution.

## 3. Walk-forward / OOS gate

Run:

```bash
python3 scripts/walk_forward_v4.py \
  --ledger runs/paper_v4_live/bundle_ledger.csv \
  --output runs/paper_v4_live/walk_forward.json
```

The evaluator uses chronological expanding history with an embargo between calibration and test. Candidate edge thresholds are selected **only on calibration data** by maximizing

\[
\bar r_{cal} - SE(\bar r_{cal}),
\]

then frozen on each test fold.

Reported metrics include:

- realized gross PnL, fees, slippage, and net PnL;
- trade-level mean return, Sharpe-like and Sortino-like statistics (not annualized);
- hit rate and profit factor;
- capital turnover;
- maximum drawdown on the supplied starting capital;
- per-strategy results;
- 1.5x execution-cost stress by default;
- circular block-bootstrap one-sided p-value;
- positive-fold stability.

The default tiny-pilot gate requires at least 30 selected OOS trades, positive normal and stressed PnL, drawdown <= 10%, profit factor >= 1.10, bootstrap p-value <= 10%, and a majority of active folds positive. These are research gates, not guarantees of future returns.

Only after the OOS result is fixed does the script learn a `production_threshold` from the latest closed historical calibration slice for the *next* forward pilot.

## 5. Tiny real-money pilot

The repository contains `scripts/tiny_live_pilot.py`, but it is deliberately **dry-run by default**.

A normal call:

```bash
python3 scripts/tiny_live_pilot.py \
  --report runs/paper_v4_live/walk_forward.json \
  --intents runs/paper_v4_live/intents.csv
```

performs the OOS gate, chooses at most one current complete bundle, resolves current token IDs/books, enforces the hard caps, and prints the proposed orders without authenticating or submitting anything.

The hard source-code caps are:

- at most **$10 total bundle notional**;
- at most **$5 per leg**;
- one bundle per process invocation;
- post-only maker entry;
- at most 10% of displayed bid queue by default;
- 95% minimum completion across every leg;
- $5 maximum live incomplete-leg-risk proxy.

Real submission additionally requires all of:

```bash
pip install py_clob_client_v2
export PK='...'
export CLOB_API_KEY='...'
export CLOB_SECRET='...'
export CLOB_PASS_PHRASE='...'
export POLYMARKET_LIVE_ENABLE=I_UNDERSTAND_REAL_MONEY
python3 scripts/tiny_live_pilot.py --execute ...
```

Credentials are read only from environment variables and must never be committed.

On execution the pilot posts GTC `post_only` maker buys, polls order state, cancels all residual orders, and attempts a FOK market sell for every matched leg after completion or execution timeout. If an unwind cannot be confirmed, it exits non-zero and writes a `CRITICAL` unresolved exposure record to `pilot_log.jsonl` rather than pretending the position is flat.

This tiny pilot is intentionally not enabled by `paper_v4_loop.sh` and must never be started automatically by CI or by the hourly edge-monitoring task.
