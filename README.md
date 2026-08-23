# Polymarket Quant Engine

C++20 live-data **paper-trading** engine for Polymarket. It implements the research architecture as a modular probabilistic trading system rather than a domain-specific betting bot.

## Current status

The executable can:

- discover active/open Polymarket markets from Gamma;
- fetch YES/NO CLOB order books in batches;
- compute true best bid/ask and walk displayed depth for paper VWAP fills;
- run microstructure, rolling logit-PCA/stat-arb, conservative NegRisk graph normalization, semantic relative value, and external-signal experts;
- combine experts with confidence weights and adaptive Brier reliability state;
- compute executable edge after CLOB V2 fee curves, displayed-book impact, extra slippage, and model uncertainty;
- size positions with fractional Kelly subject to market/event/gross exposure limits and an ex-ante drawdown-loss budget;
- paper-enter and paper-exit positions, settle resolved held markets, persist state across restarts, and emit signal/fill/history/status logs.

There is **no authenticated order submission code** in this repository. It cannot place real-money orders. That is intentional: production execution is a separate adapter and should only be added after the paper engine has been validated.

## Build

Ubuntu/Debian:

```bash
sudo apt-get update
sudo apt-get install -y build-essential cmake pkg-config libcurl4-openssl-dev libboost-all-dev python3
cmake -S . -B build -DCMAKE_BUILD_TYPE=Release
cmake --build build -j
ctest --test-dir build --output-on-failure
```

## One-shot live scan

```bash
./build/polymarket_engine \
  --config config/paper.example.json \
  --once --scan-only --markets 50
```

This reads live Gamma/CLOB data and writes diagnostics without opening paper positions.

## Continuous live paper trading

```bash
./scripts/run_paper.sh
```

or:

```bash
./build/polymarket_engine --config config/paper.example.json --loop --paper
```

Stop with `Ctrl-C`. Reusing the same `run_dir` reloads cash, open positions, peak equity, expert forecasts and rolling PCA history.

## Runtime state

Default files under `runs/paper/`:

- `signals.csv` — fair values, uncertainty, executable edge and active experts;
- `fills.csv` — simulated buys/sells/settlements;
- `history.csv` — rolling market history used by PCA;
- `broker_state.csv` — cash-account positions;
- `risk_state.csv` — peak equity and kill-switch state;
- `expert_scores.csv` — adaptive expert Brier state;
- `forecast_state.csv` — latest expert forecasts retained across restarts;
- `status.json` — latest machine-readable account snapshot.

## External information interface

`data/external_signals.csv` accepts any model producing a probability and confidence:

```text
market_key,q_yes,confidence,source,timestamp
123456,0.63,0.80,my_model,1787472000
```

`market_key` may be the Gamma market ID, condition ID, or slug. Confidence decays with a six-hour half-life. This keeps the core engine category-agnostic.

## Risk model

A candidate trade must survive executable-price, taker-fee, book-impact, extra-slippage and model-uncertainty penalties. Sizing begins with fractional Kelly and is clipped by per-trade, per-market, per-event, gross, cash and remaining drawdown-loss budgets.

The configured 15% drawdown is an **operating constraint, not a mathematical guarantee** against gaps, stale data, resolution shocks, exchange/API failures, or software defects.

## Tests

`ctest` runs deterministic unit tests and a local mock end-to-end test. The integration test starts fake Gamma/CLOB endpoints, injects an external alpha, opens a paper position, restarts the process without a duplicate fill, then resolves the market and verifies settlement and state recovery.

GitHub Actions also runs a public-API live smoke scan against Polymarket on push/PR. The live smoke job is read-only and uses `--scan-only`.
