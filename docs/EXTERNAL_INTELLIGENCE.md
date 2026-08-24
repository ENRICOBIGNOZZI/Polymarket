# Polymarket External Intelligence

`live-smoke.yml` is the read-only external-information research worker. It runs at minutes 17 and 47 of every hour, collects public/free observations, stores point-in-time history on the `telemetry` branch and performs purged chronological backtests. It replaces the former six-hour connectivity-only smoke with a strict superset: API health is still reported, but the output is now usable research evidence.

## Boundary

The worker is deliberately incapable of production execution:

- `paper_only = true`;
- authenticated execution is disabled;
- it cannot write `config/live_champion.json`;
- it cannot write production signals or execution intents;
- it cannot merge, advance `paper-validated`, deploy or submit orders;
- ambiguous contract mappings abstain rather than guessing.

Its output is evidence for the Alpha Factory. `scripts/attach_external_evidence.py` makes that evidence visible in the Alpha Factory report while preserving the incumbent recommendation and forcing every external candidate to remain `continue_shadow` until the normal FDR, incumbent-ablation, exact-CLOB-replay and integration chain has independently approved it.

## Source families

### Kalshi cross-venue probabilities

The worker scans open public Kalshi markets and conservatively maps them to liquid Polymarket contracts. A match combines token overlap, sequence similarity and expiry proximity. Critical thresholds and logical orientation must agree. The best match must beat the runner-up by a minimum margin, and confidence falls with source spread and staleness.

Accepted observations expose a point-in-time external probability, quote spread, mapping score, source timestamp and full provenance. A Kalshi price is never treated as ground truth: it must predict subsequent Polymarket moves or resolution better than the incumbent state.

### Binance crypto state

For crypto-linked contracts, public market-data-only klines produce spot returns and realized-volatility features. The worker also performs a bounded historical bootstrap by aligning hourly Binance klines with Polymarket CLOB price history. This supplies immediate research data while live snapshots accumulate.

### GDELT news state

A rotating sample of liquid markets is queried through the public GDELT DOC API. The first implementation records article intensity and tone features with retrieval/event timestamps. These features receive low prior confidence and must earn reliability through chronological evidence.

The source interface is intentionally extensible. Weather, official macro releases, polling, bookmaker odds and domain-specific official feeds can be added as separate adapters without changing the storage or backtest contract.

## Point-in-time schema

Every normalized observation contains at least:

```text
observation_id
observed_ts / retrieved_ts
market_id / event_id / question / category / end_ts
pm_bid / pm_ask / pm_mid
source / source_id / source_event_ts / source_age_seconds
feature_name / feature_value / optional q_external
confidence / mapping_score / metadata
```

The distinction among `source_event_ts`, `retrieved_ts` and the Polymarket decision timestamp is mandatory. Data published or retrieved after the decision time cannot enter that decision's features.

Persistent files on `telemetry` are:

```text
telemetry/external-intelligence-observations.jsonl.gz
telemetry/external-intelligence-prices.jsonl.gz
telemetry/external-intelligence-state.json
telemetry/latest-external-signals.jsonl
telemetry/latest-external-intelligence.json
telemetry/latest-external-intelligence.md
```

Rows are deduplicated, deterministically compressed and bounded by retention and maximum-row policies. The state records bounded-backfill progress so repeated runs do not continuously redownload the same market.

## Backtesting

For each source-feature pair and each configured horizon, the worker constructs future Polymarket labels. At decision time `t`, model parameters may use an earlier row `s` only when its complete future label was already known:

```text
s + horizon < t.
```

This is stronger than merely sorting observations and prevents overlapping-label look-ahead. The first estimator is intentionally simple and auditable:

- direct external probabilities estimate a shrinkage coefficient on `q_external - p_market`;
- continuous features use a regularized univariate predictive regression;
- parameters are re-estimated sequentially using only eligible past labels.

The OOS report includes:

- predictions and actual trades;
- predictive MSE improvement over a zero-delta baseline;
- bid/ask-aware long and short PnL;
- normal, 1.5x and 2x cost-stressed PnL;
- hit rate, profit factor and maximum drawdown per share;
- block-bootstrap one-sided p-value;
- chronological fold stability.

Historical CLOB price history does not contain full historical order books. Backfilled tests therefore use a documented synthetic spread and are marked `executable_proxy = true`. They can reject a feature or justify further research, but cannot authorize integration. A passing candidate still requires exact historical executable-price replay and an ablation against the incumbent champion.

## Reliability and admission

Each source begins with a neutral prior. Reliability updates only from OOS candidate evidence and 2x-cost-stressed results. A source with many observations but no incremental utility does not gain weight.

The Alpha Factory receives one standardized candidate record. Even a statistically passing record is attached as research-only evidence. Production admission remains:

```text
external collection
  -> purged backtest
  -> Alpha Factory/FDR and incumbent ablation
  -> research approval
  -> fresh integration/* implementation
  -> CI + monitoring + live-paper validation
  -> paper-validated
  -> paper deployment
```

Real-money execution is outside this chain.

## Manual operation

The normal schedule uses incremental collection. A bounded larger bootstrap can be requested from the workflow UI with `mode=backfill`. Pull requests and pushes use deterministic `demo` mode and never call external APIs.
