# V7 permanent data catalog

The machine contract is `config/v7_evidence_catalog.json`. Actual file inventories, allocated bytes, aliases, schemas and capture errors are immutable manifests under `runs/permanent_evidence_20260909` and the durable permanent store.

Every source below survives model and protocol replacement. Original observations are never relabeled. Gzip objects are verified against decompressed SHA-256; source revisions reference exact objects and offsets. `index.sqlite` is disposable and can be rebuilt from revisions. Unknown source families are preserved.

| Source family | Producer | Main consumers | Schema | Kind |
|---|---|---|---|---|
| canonical_ledger | polymarket_v7_fast_structural_runtime canonical ledger writer | portfolio, OMS, attribution, permanent datasets | canonical V7 execution record + economic journal | CANONICAL_ECONOMIC_SOURCE |
| pm_causal_book | polymarket_v7_maker_fillability_observer | BookTimeline, profit experiments, raw datasets | polymarket_v7_causal_book_observation_v1 | CAUSAL_SOURCE |
| pm_l2_binary | polymarket_v7_maker_fillability_observer | native book replay, raw datasets | TapeSessionHeader schema 3 + CryptoBookTapePayload schema 2; immutable token-handle manifest | CAUSAL_SOURCE |
| pm_public_trades | trade recorder / polymarket_v7_maker_fillability_observer | MakerPaperMarketEngine, flow analysis | CSV trade recorder / polymarket_v7_maker_fillability_trade_v1 | CAUSAL_SOURCE |
| external_raw | polymarket_v7_external_venue_runtime raw frame sink | external tape decoders, future ML | TapeSessionHeader v3 + RawTapeDiskRecordHeader + original payload | CAUSAL_SOURCE |
| external_normalized | polymarket_v7_external_venue_runtime | external feature kernels, replay | TapeSessionHeader v3 + TapeRecord; payload ABI version required | DERIVED_WITH_RAW_SOURCE |
| oracle_rtds | v7_rtds_external_fair_monitor.py | settlement fair, reference verification, latency diagnostics | RTDS enriched observations / polymarket_v7_rtds_rejected_frame_v1 | CAUSAL_SOURCE |
| derivative_rest | existing Binance/Deribit/Coinbase REST observers | rich contextual_features, future feature reconstruction | versioned REST observation envelopes with request/receive clocks and raw responses | CAUSAL_SOURCE |
| fair_predictions | v7_external_fair_paper_router.py | maturity, offline benchmark, signal dataset | versioned FORECAST, OPPORTUNITY_SET, VIRTUAL_FILL, VIRTUAL_FINAL records | RESEARCH_OBSERVATION |
| lead_lag | v7_external_lead_lag_collector.py | lead-lag research, PM response dataset | polymarket_v7_external_pm_lead_lag_observation_v1 | RESEARCH_OBSERVATION |
| profit_experiments | existing profit collector / report loop | profit report, cross-cohort permanent analysis | profit observation/manifest/source/settlement schemas, explicitly protocol stratified | RESEARCH_OBSERVATION |
| coordinator_decisions | v7_global_portfolio_coordinator.py | opportunity funnel, authorization attribution | polymarket_v7_global_opportunity_decision_v1 | CANONICAL_DECISION_SOURCE |
| authorization | coordinator / authorization receipt consumers | single ledger writer, opportunity funnel | typed V7 opportunity/receipt/canonical spool envelopes | CANONICAL_DECISION_SOURCE |
| maker_learning | Maker observer / v7_maker_durable_learning.py | placement model, execution dataset | versioned placement features and current-run model; underlying raw observations retained | DERIVED_MODEL_OR_FEATURE |
| markouts | polymarket_v7_maker_markout_observer | attribution, adverse selection analysis | exact fill-id joined MARKOUT research evidence | RESEARCH_OBSERVATION |
| latency | native observed latency producers | latency gates, decision-chain diagnostics | versioned latency evidence; configured constants separately ineligible | CAUSAL_SOURCE |
| point_in_time_universe | universe archiver / adaptive universe / structural scanner | market selection, historical opportunity universe | polymarket_v7_point_in_time_universe_v2 and versioned selection/relations schemas | POINT_IN_TIME_SOURCE |
| policies_models | checked-in models and policy/registry projections | fair/portfolio/risk, source provenance | explicit model/feature/policy/config/registry schema and hashes | POINT_IN_TIME_SOURCE |
| structural_candidates | polymarket_v7_fast_structural_runtime | structural research, latency diagnostics | versioned structural opportunity/latency/error CSV | CANONICAL_DECISION_SOURCE |
| quarantined_legacy | legacy research collectors | compatibility audit only | legacy explicitly quarantined schemas | QUARANTINED_PRESERVED |
| source_manifests | verified compression/evidence archiver | recovery/provenance | versioned immutable source compression records | SOURCE_PROVENANCE |
| snapshots | resolved from process manifest where available | resolved from process manifest where available | read actual schema in inventory; UNKNOWN if absent | SNAPSHOT_UNPROVEN_REPRODUCIBILITY |
| diagnostics | process named by launcher log | incident review | unstructured diagnostic log | DIAGNOSTIC_UNPROVEN_REPRODUCIBILITY |
| unclassified | UNKNOWN | UNKNOWN | UNKNOWN | UNKNOWN_PRESERVE |

The JSON catalog includes location patterns, exchange/receive/decision/publication semantics, identity field mappings, retention, compression, restart and cutover behavior for each row. Mappings describe fields that may exist; they do not fabricate historical coverage. Full depth requires its versioned binary token mapping. L1 research results remain labeled L1.

Execution model, settlement model, portfolio policy and research protocol are separate identities. Native ledger `model_sha` denotes code provenance. It is never substituted for the probability model hash. Maker metadata policy/config belongs to execution; the envelope policy/config belongs to portfolio selection.

At the first live inventory, 32,815 files were recorded. Logical byte totals include aliases, checkpoints and sparse files; capacity analysis must use allocated bytes and distinct device/inode pairs. A file disappearing between enumeration and stat is an explicit concurrent-source exclusion, not evidence that it was preserved.

Raw-data recoverability is demonstrated by the A→capture→cutover→B test, which deletes the test producer files and derived SQLite index and recovers both generations from immutable objects. Live rollout still requires a separate frozen source/flat/spool proof; the test alone does not certify deployment.

Future model-independent lead/lag origins carry the exact public input cut, derived feature schema and a SHA-256 reference. The collector persists each origin once; subsequent horizon labels refer to that immutable record. Training, forward audit and permanent dataset readers join only the matching hash, market, code and origin timestamp. Legacy complete labels remain readable. Missing referenced origins fail closed in training and remain explicit dataset exclusions.

The lead/lag journal now rotates its hot JSONL at 64 MiB. A single background worker compresses each closed segment, reads it back, verifies its decoded SHA-256 and publishes an immutable receipt before removing the redundant plain copy. A compression backlog cannot grow the hot file beyond twice the configured threshold. Readers freeze the active prefix under the rotation lock and read the prior compressed segments in order. This bounds individual active files; it does not prove that total permanent history fits in 30 GB. The updated collector has not been deployed yet.

Hybrid probability identity includes the actual external model hash, PM logit weight, clipping rule and formula version. Unknown base hashes remain unknown. Router forecasts use their configured blend weight rather than copying an unrelated monitor identity. Research prediction identities read the actual historical `research_model_model_id` and `research_model_model_hash` fields, with legacy fallback retained.
