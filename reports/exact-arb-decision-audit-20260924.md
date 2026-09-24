# Fresh economic-decision audit

Audited HEAD: `3adf41f90cf613327dc5c426065d2a442abb06c4`.
PR #1470 is open/draft; 75 commits ahead of fetched `origin/main`, zero behind.
This is a new audit, not a transfer of earlier scoped CI or local evidence to a
London profitability claim. No economic decision is supported yet.

| Feature | Current files | Status / limitation | Tests / live coverage | Next action |
|---|---|---|---|---|
| Runtime identity | paper-server-health, collection-health workflows; london_ssm_health | Conflicting hardcoded instances; scheduled SHA inferred from old deploy request | Existing SSM tests, not current live identity | One manifest + observed exact service/release identity |
| Rotation health | collection_plane_health; pure-arb-evidence-probe | Byte subtraction can become negative during rotation/retention | No rotation contract for these probes | Inode continuity + monotonic generation counters; unknown != zero |
| Canonical receipt | exporter; runtime_status; multiple SSM receipts | Health fields scattered; absent fields often coerced to zero | Existing monitoring contracts | Typed single read-only receipt with explicit missing fields |
| Exchange universe | exact_arb_exchange_universe | Public wide discovery, quarantines conflicts and invalidates errors | 28 source tests at prior SHA; latest live yield not collected | Consumer freshness, explicit metadata trust, coverage accounting |
| Warm/hot selection | exact_arb_warm_scan; exact_arb_hotset_selection | REST screen clearly nonactionable; complete WS pairs; stale successful hotset remains on error | Hotset integration tests; 64-market cap | Expiry and invalidate selection, unbiased rotating tier |
| Independent NegRisk | exchange_universe; graph automatic_negrisk_relations | Gamma membership correctly not authoritative; no independent verifier | Fixture/negative source tests only | Contract-backed membership; do not mistake mutual exclusion for exhaustiveness |
| Duplicates / algebra | exact_relation_discovery; cross_market_exact_arb_shadow; graph | Exact registry proofs, limited automatic identity, inequalities represented but not executable | Rational proof tests | Reconcile legacy scanner identity; bounded derivation and independent rules |
| Native graph | native_compile; graph_hotpath.hpp; graph native tests | Bounded evaluator/header generation, **not operational native event worker** | Release/Debug/sanitizer at earlier exact SHA | Wire isolated native worker and generation ownership, then parity/latency gates |
| Champion | pure_arb_lane.hpp; pure_arb_multi_engine.cpp; maker_fillability_observer.cpp | Frozen economics; independent observers and queues | Native lane/replay/multi-engine tests | Preserve; controlled before/after London measurements |
| Fees / sizing | graph evaluator; pure_arb_economics; exchange_semantics | Rational sizing; exact graph fee ties can differ from frozen float champion; venue shares have 2-decimal precision | Limited N-leg parity, not thousands of venue-constrained cases | Distinguish mathematical capacity and executable order quantities |
| Execution | graph_execution_shadow; pure_arb_exchange_execution_shadow | Generic N-leg transport scenarios; graph lacks binary simulator's venue/delay gates | Causal fill/unwind fixtures; no live candidates | Reuse verified terms/admission and non-atomic batch limit |
| Transformations | complete_set_merge_shadow; combo_collateral_return_shadow | Explicit verification gates; operational cost/latency unavailable | Synthetic tests | Independent current support + evidence, no instantaneous conversion |
| Combo/RFQ | combo_market_source; combo_rfq_gateway_readonly; combo_rfq_shadow | RFQ bounds not payoff attestations; optional authenticated read-only stream exists | No verified constituent live evidence | Public-only for this task; keep unsupported claims disabled |
| Arrival / lifetime | arrival_survival; deep_sizing; graph_shadow; causal | Causal as-of replay; bounded history; graph survival arms incomplete | Replay/adversarial fixtures | Complete arms, independent episodes and durable opportunity evidence |
| Resources | capital_allocator; graph_shadow ResourceLedger | Quantity reservations, no global feasible-subset optimization | Resource conflict tests | Capital scenarios without extrapolating liquidity |
| Maker | two_sided_complete_set_shadow | Joint-fill/legging model separate from locked taker payoff | No broad London matched maker evidence | Require actual queue/paired observations before maker verdict |
| Monitoring / economics | exporter; research reports | Metrics exist, no pre-registered final decision instrument | Prior local 89-second sample, only binary | Hourly health/coverage/clustered economic receipts; 8h minimum/48h cap |
| Deployment | official artifact/staging/cutover workflows | Exact-SHA gates remain mandatory; no artifact/target verified this turn | Scoped CI != full release CI | Full gates before merge/deploy; never fabricate PROMOTABLE |

Safety: PAPER only, no authenticated execution, no real submission/capital,
no automatic promotion. Engineering health and economic evidence remain separate.
The stopping protocol must preserve `decision=null` while evidence is inadequate;
one of the three requested final decisions is emitted only after its predicates
are actually supported. An external blocker is not an economic negative result.
