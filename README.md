# Polymarket Quant Engine

C++20 live-data research and **paper-trading** engine for broad Polymarket relative value and statistical arbitrage.

The design goal is not to predict isolated events. It continuously scans the active market universe and combines:

- PCA factor-residual mean reversion;
- exponentially weighted PCA;
- a sparse/robust low-rank residual model;
- hierarchical global + category factors;
- semantic/event graph residuals;
- exact binary complement arbitrage plus negative-risk consistency scanning;
- book-aware transaction costs and paper fills;
- capped fractional-Kelly sizing with a 15% drawdown risk overlay.

Real-money order submission is intentionally **not implemented in this branch**. Public Polymarket market data is used; paper fills are simulated against displayed order-book depth.

## Architecture

```text
Gamma market discovery + CLOB history/books
                  |
             warm start
                  |
        log-odds state x(i,t)
                  |
       +----------+-----------+
       |          |           |
  PCA family   graph RV   logical arb
       |          |           |
       +----------+-----------+
                  |
     net alpha after fees/spread/
      uncertainty/depth constraints
                  |
       quarter-Kelly risk allocator
                  |
      drawdown + event/token caps
                  |
       transactional paper broker
                  |
           local dashboard
```

See [`docs/MODEL.md`](docs/MODEL.md) for the mathematical design.

## Build

Ubuntu/Debian:

```bash
sudo apt-get update
sudo apt-get install -y build-essential cmake pkg-config \
  libarmadillo-dev libcurl4-openssl-dev libjson-c-dev
cmake -S . -B build -DCMAKE_BUILD_TYPE=Release
cmake --build build -j
ctest --test-dir build --output-on-failure
```

macOS with Homebrew:

```bash
brew install cmake armadillo curl json-c pkg-config
cmake -S . -B build -DCMAKE_BUILD_TYPE=Release
cmake --build build -j
```

## Run the live paper demo

```bash
./build/poly-engine --port 8080 --cycle 5 --cash 10000 --web-root web
```

Then open:

```text
http://127.0.0.1:8080
```

Useful options:

```text
--cash 10000
--cycle 5
--port 8080
--max-markets 5000
--min-liquidity 25
--web-root web
```

At startup the engine discovers active CLOB markets, warm-starts the factor models from the batch price-history endpoint, then refreshes order books in batches. The dashboard exposes equity, P&L, drawdown, gross exposure, fees, live signals and positions.

## Risk conventions

The project objective is to maximize net out-of-sample return subject to a **15% maximum-drawdown target**. The current online controller uses:

- quarter-Kelly sizing as a conservative return-maximizing proxy;
- 4% equity maximum per token;
- 12% equity maximum per event;
- 1.5% equity maximum new trade;
- 150% gross-exposure ceiling;
- liquidity/depth caps;
- progressive deleveraging from 6% drawdown;
- severe deleveraging above 12%;
- emergency flatten and zero new risk at 13.5%, leaving a buffer to the 15% MDD target.

A 15% drawdown cannot be guaranteed under jumps, resolution shocks, API outages or non-atomic execution. The internal controller therefore begins reducing risk well before 15%.

## Execution realism

Paper fills are not marked at midpoint. Buy orders walk displayed asks and are rejected when the requested size cannot be filled inside the slippage limit. Taker fees use the documented category fee curve

```text
fee = shares * feeRate * p * (1-p)
```

and displayed depth limits sizing. Exact binary arbitrage baskets are quoted and committed transactionally in paper mode: if any leg lacks depth, the whole basket is rejected. Negative-risk multi-outcome inconsistencies are displayed but do not enter paper P&L until outcome-set completeness is explicitly verified.

## Current scope

Implemented:

- broad market discovery;
- batched CLOB books;
- batched six-hour historical warm start;
- five statistical relative-value engines;
- exact binary basket scanner + guarded negative-risk consistency scanner;
- transaction-cost-aware BUY/SELL paper broker with alpha-decay exits;
- drawdown-aware portfolio sizing and emergency flatten;
- local live dashboard;
- C++ unit tests + GitHub Actions CI.

Not yet enabled:

- authenticated real-money order submission;
- authenticated user WebSocket/order lifecycle;
- persistent restart recovery;
- news/LLM semantic inference beyond metadata graph construction;
- production-grade atomic live basket execution.

Those are deliberately separated from the research/paper path so that adding credentials cannot accidentally turn the demo into a live-money bot.
