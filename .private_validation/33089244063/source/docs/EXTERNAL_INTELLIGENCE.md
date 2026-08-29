# External Intelligence worker

`external-intelligence.yml` is the point-in-time public-data research worker for the single Polymarket champion. It runs every 30 minutes at minutes 17 and 47, stores compact normalized history on the `telemetry` branch, and exposes only research evidence to the Alpha Factory.

## Purpose

The worker answers a narrow question:

> Does a public external signal contain incremental information about a Polymarket contract after event-time alignment, source/mapping uncertainty, executable quotes, fees/slippage proxies and chronological OOS validation?

A disagreement with Polymarket is not evidence of alpha. A source is useful only when it improves prediction or executable markout OOS and remains positive under cost stress.

## Initial free/public sources

| Source | Role | Output |
|---|---|---|
| Polymarket Gamma + CLOB | target universe and point-in-time market prices | active contracts, current bid/ask/mid, bounded historical price backfill |
| Kalshi public market data | cross-venue probability source | `q_external` only after conservative text/threshold/orientation/expiry matching |
| Binance public market-data-only API | crypto state | 5-minute/1-hour/24-hour returns and 24-hour realized volatility |
| GDELT DOC API | general news state | rotating market-specific article count and tone features |

No scraper requiring credentials or a paid API is enabled. New adapters must preserve point-in-time availability, source provenance and explicit abstention.

## Data contract

Every normalized observation records at least:

```text
observation_id
observed_ts / retrieved_ts
market_id / event_id / question / category / end_ts
Polymarket bid / ask / mid
source / source_id / source_event_ts / source_age_seconds
feature_name / feature_value / optional q_external
confidence / mapping_score / metadata
```

The durable state is:

```text
telemetry/external-intelligence-observations.jsonl.gz
telemetry/external-intelligence-prices.jsonl.gz
telemetry/external-intelligence-state.json
telemetry/latest-external-signals.jsonl
telemetry/latest-external-intelligence.json
telemetry/latest-external-intelligence.md
```

History is deduplicated, compressed deterministically, retained for 180 days and bounded by row caps.

## Matching and validation

Cross-venue probability matching uses:

- token overlap and containment;
- title sequence similarity;
- critical threshold agreement;
- above/below and negation orientation;
- expiry consistency;
- best-vs-second candidate margin;
- external spread and timestamp freshness.

A critical-number mismatch, orientation mismatch, expiry mismatch, weak score, ambiguous top match or low confidence produces abstention. Numeric identifiers appearing only in settlement rules do not override the contract strike comparison.

## Historical bootstrap

The first cycles backfill a bounded number of crypto-linked markets using:

- Polymarket CLOB hourly price history;
- Binance hourly candles;
- a conservative synthetic historical spread because full historical order books are not available from that endpoint.

This creates immediate research history for crypto features. The result remains an executable proxy until exact historical book replay is available.

## Backtest contract

For each source/feature and 1-hour, 6-hour and 24-hour horizon, the worker:

1. aligns the first future Polymarket quote after the horizon;
2. trains only on labels whose future horizon elapsed before the current decision timestamp;
3. estimates an expanding ridge slope, or a shrinkage coefficient for direct probabilities;
4. trades only when predicted movement exceeds the current spread hurdle plus extra costs;
5. marks YES entries at ask/future bid and NO entries through the corresponding YES bid/future ask relation;
6. repeats at 1x, 1.5x and 2x extra-cost assumptions;
7. reports PnL/share, hit rate, profit factor, drawdown, MSE improvement, fold stability and circular-block bootstrap p-value.

A challenger gate requires enough OOS predictions and trades, positive normal/1.5x/2x PnL, predictive MSE improvement, stable temporal folds and bootstrap evidence.

## Alpha Factory handoff

`attach_external_evidence.py` reads `latest-external-intelligence.json` after the normal Alpha Factory run. It can append one `continue_shadow` research candidate and one next experiment. It deliberately cannot:

- alter the Alpha Factory recommended or active canary;
- claim FDR approval for itself;
- set `integration_evidence_pass=true`;
- edit `config/live_champion.json`;
- change portfolio/risk/OOS gates;
- deploy or submit an authenticated order.

Even a passing external proxy requires exact executable CLOB replay and incumbent ablation before normal research governance can consider integration.

## Typical market routing

The generic schema supports category-specific adapters without changing the core engine:

- politics/elections: polls, official results feeds, forecasting models;
- sports: bookmaker-implied probabilities, injuries, lineups, rankings and weather;
- crypto: spot, derivatives-implied distributions, funding, on-chain and exchange state;
- macro: official releases, surveys, futures and yield curves;
- weather: forecast ensembles and observed stations;
- general events: official sources, high-quality news and cross-prediction-market probabilities.

Adapters should be added only when source terms permit the intended use and the point-in-time/history contract can be tested. Brittle scraping is not an acceptable default.

## Safety boundary

The configuration hard-codes:

```json
{
  "paper_only": true,
  "allow_authenticated_execution": false,
  "allow_direct_champion_mutation": false,
  "allow_production_signal_write": false
}
```

Workflow and unit tests fail if those invariants change. The worker is an evidence producer, not a production authority.
