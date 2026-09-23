# Unified exact-arbitrage research result — 2026-09-23

## Verdict

**Substantial implementation completed; NOT a SOTA merge candidate.** No merge,
deployment, authenticated request, order submission or automatic promotion was
performed. All five zero-authority safety flags remain in the generated artifacts.
The frozen `include/pm/v7_pure_arb_lane.hpp` has no changes.

Architecture, engineering, evidence and economic performance are separate gates:

| Dimension | Assessment |
|---|---|
| Architecture | Stronger proof/control/native/shadow separation; incomplete native event-worker integration and verified transformation breadth |
| Engineering | New causal, numeric, recovery and property coverage; not fully green platform matrix; nonzero-fee graph slower than champion |
| Evidence | Actual public universe and bounded local WebSocket observations; **not London** and not an exact-SHA release |
| Economic performance | No accepted candidate in this sample; no demonstrated opportunity-flow expansion or realized profitability |

## Repository and scope

- Branch: `research/unified-exact-arb-graph`.
- Committed HEAD: `1be49edd0cbd219eedd164cd76a0d01d1be87ab1`.
- 33 existing commits ahead of the locally available `origin/main`; local `main`
  is stale and must not be used for that comparison.
- This implementation is an **uncommitted worktree patch**, not an exact-SHA
  artifact. The baseline SHA in observations is a lineage label, not proof that
  the changed binaries were built from that committed tree.
- Changes cover compiler/discovery/universe, native evaluator/compiler/benchmark,
  causal reconstruction/replay, graph/execution shadows, observer evidence,
  monitoring/runtime manifests, tests and these reports. No production service
  or official deployment artifact was changed remotely.

## Verified coverage

The read-only [yield report](unified-exact-arb-live-yield.json) used real canonical
public Gamma metadata: **66 active markets, 132 nodes, 66 verified binary complete
sets**. Cross-market duplicate, N-way, NegRisk, Combo and transformation counts
were all zero. This is a crypto-filtered universe, not the entire exchange.

Binary verification is derived from canonical same-condition CTF outcomes and
two unique token identities, including UP/DOWN aliases. It is not an independent
on-chain attestation. No title similarity establishes equivalence. A template
settlement hash alone no longer enables cross-market duplicate discovery;
independently verified rule identity is required. All 66 rows lacked that rule
identity and N-way/NegRisk partition attestations. Zero reported unverified
relation objects therefore does **not** mean the source has exhaustive semantics.

The compiler preserves exact finite-state equality/inequality proofs and source
identity, normalizes economic paths, hashes immutable generations and validates
model/dependency integrity. N-way relations remain hyperedges. NegRisk membership
must explicitly enumerate the entire verified set. Merge/split/redeem/conversion
cost, capacity, expiry and lock-time are separate from a payoff proof.

## Native correctness and performance

The off-path native compiler emits immutable arrays and dependency handles.
The evaluator supports BUY and inventory-backed SELL, full-depth synchronized
breakpoints, coefficients, minimum sizes, partial capital bounds, freshness,
skew, lineage and overflow checks. No JSON, allocation, file or network operation
is introduced into native evaluation. The operational graph shadow is still
Python; this is not a claim that the native worker is deployed.

Native fee accounting supports rates on a 1e-9 lattice and integer exponents
0–2, with integer arithmetic and venue 5-decimal rounding. Unsupported terms
are excluded explicitly. Very large products fail closed on overflow. Quantity
is integer microshares; final monetary totals still use doubles before output
quantization, so this is not a fully fixed-point monetary engine.

A direct 40-case Python/native comparison covers 2/3/4/8/16 legs, BUY/SELL,
rational coefficients, three depth levels, fee exponents 0/1/2 and accepted as
well as rejected economics. Quantities and consumed levels agree; monetary
outputs are checked within one microcurrency unit against the rational oracle.
Non-executable fractional level dust is discarded conservatively, rather than
allowing it to stop a sweep before deeper liquidity.

The comparison found a real rounding-boundary discrepancy with the unchanged
champion: two shares at price 0.175, rate 0.06, exponent 1 have an exact fee
0.017325, rounded to 0.01733; binary floating point can produce 0.01732 in the
champion. The graph follows the rational oracle on this boundary. Ordinary
BUY/SELL parity cases remain tested, but universal bitwise champion parity is
not asserted. The zero-fee binary specialization still calls the frozen sweep.

The [actual native benchmark](unified-exact-arb-native-benchmark.json) runs
200,000 iterations per measurement with four levels, warmup and output sinks.
Every varied input is checked as accepted outside the timed section. No timed
I/O or fabricated arithmetic champion baseline remains.

| Evaluation | p50 ns | p99 ns | p99.9 ns |
|---|---:|---:|---:|
| Frozen champion binary sweep | 125 | 167 | 167 |
| Graph binary full-depth | 417 | 459 | 500 |
| Graph 3-leg | 583 | 625 | 667 |
| Graph 4-leg | 750 | 792 | 834 |
| Graph 8-leg | 1500 | 1584 | 1625 |
| Graph 16-leg | 3000 | 3083 | 4208 |
| Graph binary including lookup/dispatch | 459 | 541 | 542 |

Binary p99 is **2.75x** the champion sweep, with extra graph guards, generalized
sizing and exact fee rounding. This is a material relative slowdown and remains
a performance gate, despite sub-microsecond binary compute. These are local
microbenchmarks, not end-to-end venue latency. Clock quantization is about 42 ns;
lookup measurements should not be interpreted as exact machine-cycle costs.

## Local causal observation

The [observation artifact](unified-exact-arb-local-observation.json) is the source
of truth for the final replay funnel, global minima and rolling quantiles.
The capture contains **380,898 updates in about 89.4 seconds**, 2,433 full-depth
anchors and canonical causal level deltas. Native hot-output and deep-evidence
queue drops were both zero. This short sample cannot establish a stable hourly
opportunity rate or London economics.

| Graph funnel | Count |
|---|---:|
| Relations evaluated | 375,581 |
| Books / lineage ready | 348,476 |
| Fee and tick terms ready | 343,053 |
| Freshness ready | 342,697 |
| Leg skew ready | 339,332 |
| Raw positive | 1 |
| After-fee positive | 0 |
| After-reserve positive | 0 |
| Candidates | 0 |

Champion candidates and raw positives were both zero on its own admission gates.
The graph's one raw positive was sub-fee, not an executable improvement. Rejects:
339,332 nonpositive after costs, 27,105 missing/invalid lineage or books, 5,423
changed ticks, 3,365 leg-skew and 356 stale books. Reconstruction detected zero
sequence gaps. Different admission gates mean raw-positive counts are not a
matched-champion superiority claim.

Distance is quote currency per relation unit; negative means apparent raw edge.
Minima cover the entire replay; quantiles cover the last 100,000 observations of
each metric, explicitly not the full capture distribution.

| Distance | Global minimum | p0.1 | p1 | p5 | p50 |
|---|---:|---:|---:|---:|---:|
| Raw | -0.01000 | 0.00600 | 0.01000 | 0.01000 | 0.01000 |
| After fees | 0.00335 | 0.00670 | 0.01206 | 0.01473 | 0.04312 |
| After reserve | 0.00385 | 0.00720 | 0.01256 | 0.01523 | 0.04362 |

After-reserve distance minimum is 38.5 basis points of the unit guarantee. The
separately measured tick minimum is 1.256 ticks; tick and absolute minima need
not belong to the same event because tick sizes differ. In the rolling window,
54.038% are within one raw tick, but **none** are within one tick after fees or
reserve; 10.953% are within two ticks after reserve. Thus fees, ordinary spread
and narrow verified breadth are supported explanations. Reserve alone is not
the binding explanation, and depth/capital/partial-fill performance cannot be
inferred without after-cost candidates.

An important feeder defect was found: the original deep tape was populated only
after positive champion triggers. A separate three-minute observation recorded
894,186 updates, zero candidates and no deep snapshots, leaving the graph without
evaluation evidence. The first continuous full-book experiment then overloaded
the deep queue. It was superseded by full anchors plus existing level deltas;
that final encoding recorded zero drops. Oversized diagnostic captures were
compressed reversibly in `/tmp`, not deleted.

The graph has no additional verified family in this sample, so it cannot prove
that greater relation breadth increases opportunity flow. Ordinary quoted binary
spreads, fee drag and a narrow verified universe are supported explanations.
The observed reserve stays fixed at 0.0005 per relation unit. Raw positives must
not be equated with executable candidates; timing, semantics and fees are separate
gates. No profitable candidate was recovered by relaxing any of them.

Native champion decision p99 during the final capture was 583 ns; queue-wait p99
was 31.9 ms. A different earlier capture had 208 ns and 3.39 ms respectively.
These were sequential, differently loaded runs, with builds/replays/compression
sharing the machine. **They do not establish champion latency non-regression.**

## Execution, resources and unavailable estimates

The graph execution shadow is no longer a binary wrapper. It supports causal
N-leg sequential, parallel and explicitly non-atomic batch scenarios, requested/
available/filled quantities, arrival fee/tick/expiry checks, partial exposure,
opposite-side unwind and separate transformation stages. It labels its evidence
`TRANSPORT_SCENARIO_NOT_VERIFIED_VENUE_EXECUTION`: venue-specific availability,
taker delays and order semantics are not yet comprehensively integrated.

Full basket fills imply locked counterfactual payoff, **not realized collateral
or successful conversion**. Transformation success and unresolved exposure keep
realized PnL unavailable. SELL requires reserved inventory; no synthetic shorting
is assumed. Resource reservations are quantities, persist across event timestamps
and release only at the declared lock horizon (indefinitely if unknown).

With zero emitted candidates, 1/5/10/25/50 ms survival, all-leg/partial-fill
probabilities, unwind loss, realized PnL and capital-time efficiency are **not
estimable**, not zero. Maker quote-improvement distance is reported; paired-fill
probability, second-leg time and expected unwind cost remain null until joint
passive-fill observations exist. Independent fill probabilities are not multiplied
to manufacture an estimate.

## Validation and remaining blockers

- Full Release and Debug builds completed.
- Full Release and Debug CTest runs: **382/386 passed**. The remaining failures
  are outside the changed graph implementation: a monitoring test expects a
  literal `\\n` rather than a newline; a Linux affinity test assumes
  `os.sched_getaffinity` exists on macOS; two research files lack NumPy in CMake's
  selected Python 3.14. Those two research files pass all 12 tests using the
  installed project Python 3.9/NumPy environment (with existing numeric warnings).
- The selected graph/universe/security/single-writer/SOTA/monitoring Python suite
  passed **104 tests**; final graph-only rerun passed 41 tests.
- Native graph, frozen champion and replay parity targets passed in Release,
  Debug and ASan/UBSan. Properties include 1,000 native capacity cases, 250 Python
  sizing cases and 100 exact-proof/invalid-mutation cases.
- Adversarial coverage includes missing fees, future clocks, lineage reset,
  truncated/unknown depth, changed fees/ticks, generation corruption, causal
  arrival selection, transformation terms, resource conflicts, restart dedup,
  atomic native capacity rejection and tape rotation.
- The full-history [security audit](unified-exact-arb-security-audit.json) found
  zero pattern/entropy secret findings and confirmed checked-in live caps are
  zero. Its verdict remains `MORE_EVIDENCE_REQUIRED`: credential rotation,
  hosted-repository protections and private operational controls are external
  unverified gates, not silently treated as passed.

Promotion still requires: a genuine native event-worker integration; verified
non-binary source semantics and source-derived transformations; venue-specific
execution gates; controlled performance/no-regression evidence; broader recovery
across generation archives; a green supported-platform build/test matrix; and a
longer causal London observation. No London access or official exact-SHA research
artifact was established in this session. Local `aws`/`gh` tooling was unavailable.
No fake PROMOTABLE bundle was produced, and no merge or deployment was attempted.
