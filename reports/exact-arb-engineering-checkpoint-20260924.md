# Exact-arbitrage engineering checkpoint — not a final economic report

Historical checkpoint. Later worktree evidence is in
[fee attribution and public settlement observations](exact-arb-fee-attribution-checkpoint-20260924.md)
[independent NegRisk observation](exact-arb-negrisk-attestation-checkpoint-20260924.md),
with the subsequent [causal shadow capital lifecycle](exact-arb-capital-lifecycle-checkpoint-20260924.md).
These do not close the remaining technical gates or the London economic study.

Branch: `research/unified-exact-arb-graph`; PR #1470 remains draft.
Initial audited commit: `3adf41f90cf613327dc5c426065d2a442abb06c4`.
Current base commit: `01950279cda4b531b2c302806d68b605864c9a7b` (audit report commit).
The implementation below is in the working tree, not an exact-SHA deployed release.
The public experiment used that base SHA as a model label. It is **not** a
PROMOTABLE artifact, a London observation, or a proof of profitability.

## Implemented in this checkpoint

- One London target manifest; health workflows resolve the observed service
  model/release SHA instead of historical instance IDs, deployment requests,
  or workflow HEAD. Explicit requested SHA mismatches fail closed. The target
  comes from the existing exact-deploy configuration, not a new live attestation.
- Shared canonical health builder, exporter `/runtime-health.json`, SSM receipt,
  Prometheus checks and alert. Missing counters remain null/NaN. Identity,
  freshness, clock, fencing, workers, drops and PAPER boundary are checked.
  Graph health and economic evidence are separate from champion health.
- Rotation-aware evidence probes: inode continuity, writer generation, row
  counters, retained segments, truncation detection and retention-gap accounting.
  A shrinking current file is not negative ingestion. Neighbouring journals
  cannot manufacture growth. Storage directory disappearance is race-safe;
  permission failures and a missing storage root still fail.
- Source leases for live graph/warm/hotset consumers. Source failure atomically
  invalidates prior outputs. An unchanged cached graph still expires. Native
  research-only selection checks the source lease. Offline helpers can use an
  explicit as-of clock without pretending old fixtures are live.
- Public Gamma keyset discovery: `/events/keyset` with `after_cursor`, duplicate
  and cursor-cycle checks, bounded page budget and explicit partial coverage.
  The previous `/events` offset scan actually failed at offset 2100 with HTTP
  422 directing the caller to `/events/keyset`.
- Off-path compiler market-ID, claim-collision and settlement indices replace
  quadratic scans; ambiguous selectors remain rejected.
- Graph survival arms now include 2 and 100 ms; near-distance quantiles include
  p0.01/p95 and the 5-tick fraction.
- Fixed pre-existing repository gate defects: systemd reset-line assertion,
  obsolete fixed process count (now validated against the canonical manifest),
  macOS Linux-affinity fixture, and decimal spread subtraction in the offline
  maker challenger. Frozen PureArb economics were not changed.

## Public-source experiment, not executable evidence

The 100-page, 45.033-second bounded keyset scan returned:

| Quantity | Observed |
|---|---:|
| Events | 10,000 |
| Normalized orderable markets | 105,222 |
| Unique tokens | 210,444 |
| Compiled graph nodes | 100,484 |
| Compiled relations | 50,242 |
| Relation families compiled | Same-market binary complete sets only |
| NegRisk event candidates | 4,448 |
| Independently verified NegRisk complete sets | 0 |
| All graph unverified candidates | 90,906 |

Pagination did **not** reach the end; this is not 100% exchange coverage.
Compiled binary payoff identities use canonical API condition/token mappings,
not independent on-chain token attestations. Enabled relations still require
valid causal books, fee/depth/size/inventory/resource gates. No candidates were
measured by this metadata experiment; the candidate count is unknown, not zero.

Universe membership hash:
`43c6fa059e7e77ae85ac8561780139d48a0b5f55dc4efce9dd94b20c281c1b1f`.
Graph generation:
`6376dfffc7e613d8c38e525787a8a5fa762fed0bd277fbb149937c036f721125`.
Local, temporary raw artifacts: `/tmp/polymarket-exact-keyset.1aFFvz/`.
These have not been archived to the research evidence store.

## Semantic finding

Reading the authoritative adapter source establishes **at most one YES**;
it does not establish that at least one question must resolve YES. The question
preparation path can append members; current membership is not a freeze proof.
Therefore matching Gamma members or a pinned on-chain question count cannot by
itself enable a constant-payout NegRisk complete set. Conversion semantics,
augmentation/Other behaviour, deployed-bytecode identity and operational
readiness still need separate attestations. Sources:
[NegRiskAdapter](https://raw.githubusercontent.com/Polymarket/neg-risk-ctf-adapter/main/src/NegRiskAdapter.sol),
[MarketDataManager](https://raw.githubusercontent.com/Polymarket/neg-risk-ctf-adapter/main/src/modules/MarketDataManager.sol),
[Polymarket negative-risk documentation](https://docs.polymarket.com/concepts/negative-risk).
No independent verifier or new verified NegRisk family was completed here.

## Local benchmark

Existing native benchmark, 200,000 iterations, four depth levels, local macOS:

| Compute path | p99 ns |
|---|---:|
| Actual champion binary sweep | 125 |
| Graph binary sizing | 333 |
| Graph 3-leg sizing | 417 |
| Graph 4-leg sizing | 542 |
| Graph 8-leg sizing | 1,000 |
| Graph 16-leg sizing | 1,834 |
| Dependency lookup | 42 |

Binary graph/champion p99 ratio: 2.664. This is neither a London latency estimate
nor a side-by-side champion non-regression test. No speed/profitability claim
or promotion is warranted.

## Still required before a mature deployment

Local validation of this working tree:

- Python: **2,355 passed, 1 skipped**, 384 numerical warnings from existing
  research model tests. No skip was added to obtain green.
- Full configured Release CTest: **394/394 passed**.
- Native graph/champion/replay: **3/3 each** in Release, Debug and combined
  AddressSanitizer/UndefinedBehaviorSanitizer builds.
- Changed C++ observer built successfully in Release (three existing warnings).
- Storage/offload regression: **20 consecutive CTest repetitions passed** after
  reproducing and fixing the disappearing-directory failure.
- Workflow shell parse, runtime identity, source expiry, pagination, process
  manifest, monitoring and safety contracts are included in the local suites.
- `git diff --check` passed. Frozen PureArb lane/economics/multi-engine files
  have no changes. Fetched main is zero commits ahead of current base.

These are local results, not a new Linux CI run or release-artifact attestation.

The full gap matrix remains in `exact-arb-decision-audit-20260924.md`.
In particular:

1. Operational native graph event worker and atomic generation ownership, not
   Python evaluation per event. The public graph exceeds the native compiler's
   65,536-node bound: causally selected subgraphs/resource-bounded partitioning
   are needed; simply increasing the bound is not a solution.
2. Independent semantic attestations, broader verified families and bounded
   derivation/deduplication. Metadata breadth is not relation breadth.
3. Venue-verified N-leg execution admission, precision/delays, partial-fill
   exposure, unwind, transformation and capital-time evidence.
4. Thousands of champion parity cases, deterministic replay across activation
   and source/generation transitions, unbiased causal coverage weights, full
   funnel/lifetimes and a separate causal maker counterfactual.
5. Pre-registered meaningful PnL/capital thresholds and cluster-aware stopping
   analysis. Do not infer independence from update count.
6. Exact-SHA Linux CI/artifact gates, official deployment and London champion
   before/after measurements. Local green tests are not these gates.

## External access and economic evidence

Read-only SSH to the configured London address `100.104.183.109` was rejected
for both the configured `enrico` user and the deployment `ubuntu` user.
There is no local AWS profile/configuration or AWS credential environment, and
the connected GitHub tools do not expose workflow dispatch. Integration search
found no usable AWS/SSM connector. The canonical target is
`i-04042ca7da7a23215` in `eu-west-2`, but its actual current identity/health has
**not** been independently observed in this session.

Required next external action: enable an authorized AWS/SSM session for that
runtime, or restore the approved SSH access. Do not paste secrets into chat.
No merge, deployment, authenticated order, real capital use, champion authority
change or automatic promotion was performed.

London causal hours, relation evaluations, raw/after-cost positives, fill rates,
unwind losses, PnL, capacity and coverage remain **unavailable** for this study.
The minimum eight-hour and event/exposure conditions have not been satisfied.
`research_decision = null`: this checkpoint is not any of the three final
research decisions. Access failure is not evidence that exact arbitrage has
negative economic value. Do not freeze the branch as an economic falsification.
