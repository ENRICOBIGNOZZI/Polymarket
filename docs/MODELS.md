# V7 Models

The authoritative family registry is `config/v7_strategy_registry.json`; the implementation/evidence matrix is `config/v7_capability_matrix.json`.

Models are grouped by economic mechanism and frequency. HFT-0 contains maker, structural, settlement fair/informed taker and sports latency. FAST-1 contains graph/RV, micro-taker, market-open and eligible cross-platform work. EVENT-2 contains OSINT and wallet intelligence. SLOW-3 contains ranking, PCA and local factor.

Settlement fair predicts the exact contract resolution condition, not generic asset direction. Structural and cross-venue arbitrage require deterministically verified semantics. Ranking is relative. PCA and local factor use current coherent executable books, frozen training transformations and explicit common-factor exposure.

No generic autonomous ML bot exists. Challengers must beat simple causal benchmarks chronologically.

OSINT likelihood ratios are fitted only on deduplicated root lineages with a
strict chronological cutoff and both binary outcomes represented. OOS status
requires a later independent-event block and Brier improvement over the frozen
pre-event probability. Source operating statistics checked in as
`PRIOR_UNVALIDATED` are not empirical evidence and cannot authorize execution.
`scripts/v7_osint_likelihood.py` writes an exact-SHA frozen challenger registry;
even an OOS-valid entry has `execution_authority=false` until event-market,
edge-decay, race and forward economic gates are separately satisfied.
