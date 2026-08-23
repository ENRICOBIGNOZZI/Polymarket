# Polymarket Universal Quant Engine

C++20 **live-data / paper-trading** engine for scanning Polymarket across categories. The core is category-agnostic: every contract is treated as a probabilistic event and routed through multiple alpha experts rather than hard-coding election, sports, crypto, macro, or other domain strategies.

## Architecture

`Gamma discovery -> batched CLOB books -> universal representation -> experts -> adaptive ensemble -> executable net edge -> risk -> paper broker`

Current experts:

- **Microstructure**: midpoint, spread and near-touch depth imbalance.
- **Online PCA/stat-arb**: rolling first principal component of logit-probability changes; trades only idiosyncratic residuals after a warm-up window.
- **NegRisk graph consistency**: relative-value normalization among linked markets, with a completeness guard to avoid using obviously partial event groups.
- **Semantic relative value**: conservative hashed-text nearest-neighbour shrinkage across similar contracts.
- **External-information expert**: generic probability/confidence input, independent of domain; stale signals decay automatically.

The ensemble weights active experts by confidence and exponentially penalizes experts with worse observed Brier loss. Experts can abstain when the required information is unavailable.

## Live data

The engine currently uses public, read-only Polymarket endpoints:

- Gamma market discovery: `https://gamma-api.polymarket.com/markets`
- CLOB batched books: `POST https://clob.polymarket.com/books`
- single-book fallback: `https://clob.polymarket.com/book?token_id=...`
- per-market CLOB information: `https://clob.polymarket.com/clob-markets/<condition_id>`

`clobTokenIds` and `outcomePrices` are parsed defensively because Gamma may encode them as JSON strings. Books are scanned for the true max bid / min ask rather than assuming response ordering.

## Fees and executable edge

The engine reads the live fee descriptor `(r,e)` from CLOB market information and uses

```text
fee_per_share = r * [p * (1-p)]^e
```

with conservative fallbacks when live fee metadata are unavailable.

The alpha calculation is based on the **executable ask**, not midpoint, so spread is not subtracted twice. Expected edge is then reduced by live taker fee, configured slippage and a model-uncertainty buffer.

## Paper fills and exits

The paper broker now changes cash exactly when simulated fills occur:

- entry at ask plus configured slippage;
- live per-market fee debited from cash;
- exit at bid minus configured slippage;
- exit fee debited from cash;
- repeated entries aggregate shares by token;
- positions can close on a sufficiently strong opposite signal or when fair value falls below the executable bid.

Runtime output is persisted in:

- `runs/signals.csv` — every evaluated signal;
- `runs/fills.csv` — simulated entries/exits, fees and cash;
- `runs/history.csv` — persisted midpoint history for the PCA/stat-arb warm-up;
- `runs/status.json` — current paper equity, cash, positions, ideas and drawdown state.

## Risk

Defaults:

- hard drawdown kill switch: **15%**;
- ex-ante drawdown-room constraint using current loss from peak plus worst-case open-position loss;
- max gross exposure: 65% of equity;
- max exposure per market: 4%;
- max exposure per event: 12%;
- max single trade: 2%;
- 0.20 Kelly multiplier;
- ranking by net edge relative to model uncertainty.

The 15% control is a risk budget, not a mathematical guarantee against gap/default/execution risk.

## Build

Requirements: CMake >= 3.20, C++20 compiler, libcurl, Boost headers.

```bash
cmake -S . -B build -DCMAKE_BUILD_TYPE=Release
cmake --build build -j
ctest --test-dir build --output-on-failure
```

## Run on live Polymarket data

One read-only snapshot + paper decision cycle:

```bash
./build/poly_live --once --markets 50 --capital 10000
```

Continuous paper mode:

```bash
./build/poly_live --loop --interval 10 --markets 100 --capital 10000
```

## External information without domain hard-coding

Optional CSV schema:

```text
market_id,probability,confidence,source
123456,0.64,0.80,my_model
```

Run with:

```bash
./build/poly_live --loop --external-csv examples/external_signals.csv
```

Any future data connector only has to map outside information to `(market_id, probability, confidence, source)`. Polls, bookmaker odds, macro models, weather, crypto data, news/NLP and proprietary forecasts can therefore enter the same universal interface without changing the portfolio engine.

## CI / live smoke

GitHub Actions builds the C++ engine, runs unit tests and then performs a **read-only live Polymarket smoke test**. No API credentials or wallet are required for this path.

## Deliberately not enabled

Real-money signing/submission is intentionally excluded. It should be a separate execution adapter with credentials, explicit capital limits and a manual enable flag only after paper logs provide enough evidence on calibration, turnover, realized slippage and drawdown.
