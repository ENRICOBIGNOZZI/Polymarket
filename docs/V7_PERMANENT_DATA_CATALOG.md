# V7 Permanent Crypto Data Catalog

Generated from `config/v7_evidence_catalog.json`. Only data useful to the crypto runtime, execution research, replay or audit belongs here.

| Source family | Producer | Consumers | Schema | Source kind |
|---|---|---|---|---|
| canonical_ledger | v7_ledger_spool.py / V7_CANONICAL_LEDGER | portfolio, OMS, attribution, permanent datasets | canonical V7 execution record + economic journal | CANONICAL_ECONOMIC_SOURCE |
| pm_causal_book | polymarket_v7_maker_fillability_observer | BookTimeline, profit experiments, raw datasets | polymarket_v7_causal_book_observation_v1 | CAUSAL_SOURCE |
| pm_l2_binary | polymarket_v7_maker_fillability_observer | native book replay, raw datasets | TapeSessionHeader schema 3 + CryptoBookTapePayload schema 2; immutable token-handle manifest | CAUSAL_SOURCE |
| pm_public_trades | trade recorder / polymarket_v7_maker_fillability_observer | MakerPaperMarketEngine, flow analysis | CSV trade recorder / polymarket_v7_maker_fillability_trade_v1 | CAUSAL_SOURCE |
| external_raw | polymarket_v7_external_venue_runtime raw frame sink | external tape decoders, future ML | TapeSessionHeader v3 + RawTapeDiskRecordHeader + original payload | CAUSAL_SOURCE |
| external_normalized | polymarket_v7_external_venue_runtime | external feature kernels, replay | TapeSessionHeader v3 + TapeRecord; payload ABI version required | DERIVED_WITH_RAW_SOURCE |
| oracle_rtds | v7_rtds_external_fair_monitor.py | settlement fair, reference verification, latency diagnostics | RTDS enriched observations / polymarket_v7_rtds_rejected_frame_v1 | CAUSAL_SOURCE |
| derivative_rest | existing Binance/Deribit/Coinbase REST observers | rich contextual_features, future feature reconstruction | versioned REST observation envelopes with request/receive clocks and raw responses | CAUSAL_SOURCE |
| fair_predictions | v7_external_fair_paper_router.py | maturity, offline benchmark, signal dataset | versioned FORECAST, OPPORTUNITY_SET, VIRTUAL_FILL, VIRTUAL_FINAL records | RESEARCH_OBSERVATION |
| lead_lag | v7_external_lead_lag_collector.py | lead-lag research, PM response dataset | polymarket_v7_external_pm_lead_lag_origin_v1 + observation_v1; immutable origin hash references; legacy full rows retained | RESEARCH_OBSERVATION |
| profit_experiments | existing profit collector / report loop | profit report, cross-cohort permanent analysis | profit observation/manifest/source/settlement schemas, explicitly protocol stratified | RESEARCH_OBSERVATION |
| coordinator_decisions | v7_global_portfolio_coordinator.py | opportunity funnel, authorization attribution | polymarket_v7_global_opportunity_decision_v1 | CANONICAL_DECISION_SOURCE |
| authorization | coordinator / authorization receipt consumers | single ledger writer, opportunity funnel | typed V7 opportunity/receipt/canonical spool envelopes | CANONICAL_DECISION_SOURCE |
| maker_learning | Maker observer / v7_maker_durable_learning.py | placement model, execution dataset | versioned placement features and durable exact-policy/config cross-cutover research model; underlying raw observations retained | DERIVED_MODEL_OR_FEATURE |
| markouts | polymarket_v7_maker_markout_observer | attribution, adverse selection analysis | exact fill-id joined MARKOUT research evidence | RESEARCH_OBSERVATION |
| latency | native observed latency producers | latency gates, decision-chain diagnostics | versioned latency evidence; configured constants separately ineligible | CAUSAL_SOURCE |
| point_in_time_crypto_universe | crypto universe archiver / crypto universe | market selection, historical crypto opportunity universe | polymarket_v7_point_in_time_crypto_universe_v1 and versioned crypto selection schemas | POINT_IN_TIME_SOURCE |
| policies_models | checked-in models and policy/registry projections | fair/portfolio/risk, source provenance | explicit model/feature/policy/config/registry schema and hashes | POINT_IN_TIME_SOURCE |
| quarantined_legacy | legacy research collectors | compatibility audit only | legacy explicitly quarantined schemas | QUARANTINED_PRESERVED |
| source_manifests | verified compression/evidence archiver | recovery/provenance | versioned immutable source compression records | SOURCE_PROVENANCE |
| snapshots | resolved from process manifest where available | resolved from process manifest where available | read actual schema in inventory; UNKNOWN if absent | SNAPSHOT_UNPROVEN_REPRODUCIBILITY |
| diagnostics | process named by launcher log | incident review | unstructured diagnostic log | DIAGNOSTIC_UNPROVEN_REPRODUCIBILITY |
| unclassified | UNKNOWN | UNKNOWN | UNKNOWN | UNKNOWN_PRESERVE |

## Retention rule

The canonical ledger and unique causal crypto sources are permanent. High-volume raw detail may be retired only under the checked-in rolling-window policy after the required immutable/content-addressed evidence has been created. Derived reports can always be rebuilt from their declared sources.
