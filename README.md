# Polymarket Universal Quant Engine

C++20 live-data **paper-trading** engine implementing the category-agnostic architecture in the accompanying research note. Every binary contract is treated as a probabilistic claim and passed through the same pipeline rather than hard-coding election, sports, crypto, macro, or other domain strategies.

## Architecture

```text
Gamma market discovery
        -> batched CLOB books
        -> universal market state
        -> microstructure expert
        -> synchronized PCA / statistical-arbitrage expert
        -> event-graph / NegRisk expert
        -> semantic relative-value expert
        -> external-information expert
        -> adaptive Brier-weighted ensemble
        -> fair probability + uncertainty
        -> executable net edge
        -> fractional-Kelly + portfolio/drawdown limits
        -> persistent paper broker
```

The forecasting layer, trade decision, portfolio construction and execution are intentionally separate.

## Current experts

- **Microstructure** — midpoint, spread and near-touch depth imbalance. Confidence rises with depth and falls with spread.
- **PCA/stat-arb** — one-factor PCA of synchronized logit-probability changes. The model is inactive until sufficient common history exists and mean-reverts only the current idiosyncratic residual.
- **Graph / NegRisk** — normalizes linked mutually-exclusive outcomes only when the observed probability sum is close enough to one for the group to look complete.
- **Semantic RV** — hashed text embedding plus conservative nearest-neighbour shrinkage. This is deliberately a lightweight first implementation; a learned event encoder can replace it without changing the rest of the engine.
- **External information** — generic `(market_id, probability, confidence, source, timestamp)` interface with exponential staleness decay.

Experts may abstain. Active predictions are combined with confidence and an adaptive penalty based on each expert's realized Brier loss. Expert calibration state survives process restarts. Resolution checks are rate-limited and continue for markets that leave the active universe, so Brier weights are learned from resolved forecasts even when those markets were never traded. Non-binary/void payouts are excluded from the Bernoulli Brier update rather than being mislabeled as YES or NO.

## Live market data

The engine uses public read-only Polymarket endpoints:

- primary Gamma discovery: `GET https://gamma-api.polymarket.com/markets` with active/open filters and 24h-volume sorting;
- resilient Gamma fallback: `GET https://gamma-api.polymarket.com/markets/keyset` with cursor pagination and no mutable sort key;
- CLOB batched books: `POST https://clob.polymarket.com/books`;
- single-book fallback: `GET https://clob.polymarket.com/book?token_id=...`;
- CLOB market metadata fallback: `GET https://clob.polymarket.com/clob-markets/<condition_id>`.

The normal list endpoint is preferred while it supports the economically useful 24h-volume sort. The keyset endpoint is retained as a compatibility fallback and uses `after_cursor`; mutable sorting is deliberately avoided there because the live Gamma backend has returned HTTP 422 for `order=volume_num` even though that combination appears in the schema. Keyset results are de-duplicated and sorted locally by 24h volume.

The engine filters for active, open, order-accepting markets and then validates true two-sided order books. Best bid and ask are computed from the returned levels rather than assuming response ordering.

Transient HTTP/network/429/5xx failures are retried with exponential backoff; failed batched-book requests fall back to single-book reads.

## Fees and executable edge

For a taker trade at probability price `p`, the current public fee rule is modeled as

```text
fee_per_share = feeRate * p * (1 - p)
```

and simulated total fees are rounded to five decimal places. Live fee metadata from Gamma/CLOB are preferred; the configured conservative fallback is used only when fee metadata cannot be obtained.

Signals are measured against the **executable ask**. The engine therefore does not subtract half the bid-ask spread a second time.

```text
raw_edge = side_fair_probability - executable_ask
net_edge = raw_edge - taker_fee - slippage - other_costs - uncertainty_penalty
```

Only positive net edge above the configured threshold is admissible.

## PCA / statistical-arbitrage implementation

History is stored with timestamps. Markets enter the same PCA calculation only when their recent observation timestamps match, preventing different loop iterations from being accidentally treated as simultaneous cross-sectional observations.

For each synchronized group:

1. convert prices to logits;
2. compute logit changes;
3. estimate the first covariance eigenvector by power iteration;
4. estimate the current common shock;
5. isolate each market's idiosyncratic residual;
6. partially mean-revert the residual in logit space;
7. scale confidence using residual z-score, cross-sectional size and factor strength.

The factor model is computed once per synchronized group per cycle and cached for all markets in that group.

## Ensemble uncertainty

The model returns both a fair probability and uncertainty. Uncertainty combines weighted disagreement among active experts with a spread/liquidity proxy. Ranking is based on net edge relative to uncertainty rather than raw edge alone.

## Portfolio and drawdown control

Default controls include:

- hard maximum drawdown: **15%**;
- ex-ante remaining drawdown room after current loss from peak and worst-case open-position loss;
- max gross exposure: 65% of equity;
- max exposure per market: 4%;
- max exposure per event: 12%;
- max single trade: 2%;
- fractional Kelly multiplier: 0.20.

Kelly sizing uses the **cost- and uncertainty-adjusted executable net edge**, not the raw `q-p` discrepancy.

If the hard drawdown kill switch fires, new entries stop immediately and the paper broker attempts to liquidate every open token for which a current bid is available. The 15% level is a risk budget, not a mathematical guarantee against gaps, resolution shocks, unavailable liquidity, stale data or model misspecification.

## Persistent paper execution

Paper entry:

```text
fill = ask * (1 + slippage_bps / 10000)
```

Paper exit:

```text
fill = bid * (1 - slippage_bps / 10000)
```

Cash, positions, fees, model calibration and drawdown state are persisted after fills and at the end of every cycle. Held markets are reconciled even if they disappear from the normal tradable scan.

Settlement is deliberately stricter than a simple `closed=true` check. When `umaResolutionStatus` is available, `requested`, `proposed`, and `disputed` are treated as non-final; paper settlement is allowed only at `resolved` or `settled`. Some automatically resolved markets do not expose UMA state, so the conservative fallback requires both closure and an effectively binary terminal YES payout. At final resolution the paper broker uses the actual terminal YES payout: a void/non-binary payout such as 0.5 therefore credits YES at 0.5 and NO at 0.5, rather than pretending that one side won. Such non-binary outcomes are not fed into Bernoulli Brier calibration.

Runtime files:

```text
runs/signals.csv
runs/fills.csv
runs/history.csv
runs/broker_state.csv
runs/risk_state.csv
runs/model_state.csv
runs/status.json
```

`--fresh` explicitly deletes this paper state before startup. Do not use it when continuing an existing paper run.

## Build and tests

Requirements: CMake >= 3.20, C++20 compiler, libcurl and Boost headers.

```bash
cmake -S . -B build -DCMAKE_BUILD_TYPE=Release
cmake --build build -j
ctest --test-dir build --output-on-failure
```

The unit test suite covers Gamma legacy/keyset parsing, UMA resolution-state parsing, current fee calculations, drawdown kill logic, broker accounting/persistence, non-binary settlement, text similarity and synchronized PCA activation.

## Run live-data paper trading

Single decision cycle:

```bash
./build/poly_live --once --markets 100 --capital 10000
```

Continuous paper mode:

```bash
./build/poly_live --loop --interval 10 --markets 500 --capital 10000
```

Useful controls:

```text
--min-edge <probability edge>
--min-liquidity <USD>
--max-spread <probability spread>
--slippage-bps <bps>
--max-drawdown <0..0.15>
--external-csv <file>
--fresh
```

## External information

CSV schema:

```text
market_id,probability,confidence,source,timestamp
123456,0.64,0.80,my_model,2026-08-23T10:30:00Z
```

`timestamp` is optional and may be Unix seconds or UTC ISO-8601. If omitted, load time is used. This interface is deliberately domain-independent: polling models, bookmaker odds, macro forecasts, weather, crypto derivatives, news/NLP systems or proprietary models can all feed the same ensemble.

## CI

GitHub Actions:

1. installs C++ dependencies;
2. configures and builds the engine;
3. runs unit tests;
4. performs a read-only live Polymarket paper smoke test.

No wallet or private key is required.

## Deliberately excluded

Real-money signing/order submission is not enabled. Production execution should remain a separate authenticated adapter with explicit capital limits, reconciliation, cancel/replace handling and manual enablement after paper evidence is sufficient.
