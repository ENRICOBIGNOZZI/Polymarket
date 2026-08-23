# Polymarket Universal Quant Engine

C++20 **paper-trading** engine for scanning Polymarket across categories. The core is intentionally category-agnostic: every contract is represented as a probabilistic event and passed through multiple alpha experts rather than hard-coding election, sports, crypto, macro, or other domain strategies.

## Architecture

`Gamma discovery -> CLOB books -> universal representation -> experts -> adaptive ensemble -> net edge -> risk -> paper broker`

Current experts:

- **Microstructure**: midpoint, spread and near-touch depth imbalance.
- **Online PCA/stat-arb**: rolling first principal component of logit-probability changes; trades only idiosyncratic residuals after a warm-up window.
- **NegRisk graph consistency**: relative-value normalization among linked NegRisk markets inside the same event.
- **Semantic relative value**: conservative hashed-text nearest-neighbour shrinkage across similar contracts.
- **External-information expert**: generic probability/confidence input, independent of domain; stale signals decay automatically.

The ensemble weights active experts by confidence and exponentially penalizes experts with worse observed Brier loss. Experts can abstain when the required information is unavailable.

## Risk / execution

The executable never submits real orders. It reads live public Polymarket data and executes only in an internal paper broker.

Defaults:

- hard portfolio drawdown kill switch: **15%**;
- max gross exposure: 65% of equity;
- max exposure per market: 4%;
- max exposure per event: 12%;
- max single trade: 2%;
- 0.20 Kelly multiplier;
- edge is reduced by half-spread, assumed fees, slippage and model-uncertainty buffer.

## Build

Requirements: CMake >= 3.20, C++20 compiler, libcurl, Boost headers.

```bash
cmake -S . -B build -DCMAKE_BUILD_TYPE=Release
cmake --build build -j
ctest --test-dir build --output-on-failure
```

## Run against live Polymarket data

One read-only snapshot + paper decision cycle:

```bash
./build/poly_live --once --markets 50 --capital 10000
```

Continuous paper mode:

```bash
./build/poly_live --loop --interval 10 --markets 100 --capital 10000
```

Signals and simulated trades are written to `runs/signals.csv` and `runs/trades.csv`.

## External information, without domain hard-coding

Optional CSV schema:

```text
market_id,probability,confidence,source
123456,0.64,0.80,my_model
```

Run with:

```bash
./build/poly_live --loop --external-csv examples/external_signals.csv
```

Any future data connector only needs to map its information to `(market_id, probability, confidence, source)`. Polling, bookmaker odds, macro models, weather, crypto, NLP/news and proprietary forecasts can therefore enter the same universal interface without changing the portfolio engine.

## Live API path

The engine uses public read-only endpoints:

- Gamma market discovery: `https://gamma-api.polymarket.com/markets`
- CLOB order book: `https://clob.polymarket.com/book?token_id=...`

It parses `clobTokenIds` defensively because Gamma may encode arrays as JSON strings. The book implementation scans all levels for max bid/min ask instead of assuming response ordering.

## CI / live smoke test

Every branch push runs:

1. C++ configure/build;
2. unit tests;
3. a read-only live Polymarket API smoke test in paper mode.

## Deliberately not enabled yet

Real-money order signing/submission is intentionally excluded from this branch. It should be a separate execution adapter with credentials, explicit capital limits and a manual enable flag only after the paper engine has accumulated enough observations to estimate calibration, turnover, slippage and drawdown reliably.
