# Native diagnostics and episode evidence checkpoint

Branch: `research/unified-exact-arb-graph`.
Base SHA: `f50147e18e02ebed76cabe900b708e4d9479e603` plus uncommitted work.
This is local engineering evidence, not a release artifact or a London receipt.
PAPER-only; no real orders, execution authority, merge or deployment.

## Implemented since the WS checkpoint

- Native order sizing derives a common quantity lattice for every rational leg
  coefficient and the configured 10,000-microshare total-order quantum. It
  recomputes fees, depth and capital after flooring quantity, without rounding
  individual L2 fragments. It explicitly retains
  `global_size_optimum_proven=false`: the inherited sweep is not a proof of a
  global optimum with discontinuous fees.
- Native near-arbitrage diagnostics record raw, after-fee and after-reserve
  distance in integer nanocurrency, the fee-probe size, weighted tick, guarantee,
  leg ages/skew and readiness gates. These are top-of-book unit diagnostics,
  **not full-depth executable capacity**. Invalid values are null.
- Native observation schema v2 adds a session-monotonic observation sequence
  incremented before queue admission, a continuity serial and the conservative
  minimum of book-freshness, source and relation deadlines. Queue/disk losses are
  therefore visible even when there are no intervening written rows. Generation
  activation, source invalidation and book-lineage reset break continuity.
- `scripts/v7_exact_arb_native_evidence.py` reduces sealed native tapes off path.
  It checks model/PAPER identity, archived bundle hashes, graph/proof/handle
  identity, monotonic input order and exact duplicate consistency. SQLite stores
  deduplication keys and descriptive quantiles rather than unbounded Python
  samples. It does not traverse the graph on a market update or place orders.
- An observed negative-to-positive transition starts a diagnostic episode.
  Initial positives, reconnects, generation changes, telemetry gaps and expired
  books are censored, never counted as proven fresh arrivals. Only observed
  start/end episodes enter complete-lifetime quantiles. EOF is censored and tape
  tail completeness remains explicitly unverified.
- Per-family/direction reports contain readiness counts, positive evaluation
  counts, observed starts, positive segments, distance quantiles in PUSD/ticks/bps,
  fractions within 0.25/0.5/1/2/5 ticks, and complete/censored lifetime counts.
  Near-arb statistics are update-weighted descriptions, not independent samples.
  No repeated-update PnL is summed. Execution PnL, full-fill opportunities,
  opportunities/hour and the research decision remain null.
- CLI replay publishes an immutable content-addressed report directory containing
  both `report.json` and `episodes.jsonl`. Reprocessing overlapping rotated
  segments cannot double-count matching rows; conflicting duplicates fail closed.
  An incomplete final JSON line, excessive row/line budget or missing archive
  prevents publication. It does not yet run as an hourly production service.

## Verification

- 24 new Python evidence tests; 77 scoped evidence/graph/observer/health/manifest
  tests passed.
- Runtime, loader/WS writer, cross-language roundtrip and diagnostic native tests:
  4/4 in Release, Debug, combined ASan/UBSan and ThreadSanitizer.
- Cross-language integration now exercises the actual WS decoder and native
  writer, not only a hand-built JSON row. The injected overload/disk suppression
  creates exactly 5,000 missing observations between sequences 1 and 5,002. The
  reducer detects all 5,000 and reports two left-censored positive segments, zero
  observed new arrivals and unknown execution PnL.
- Release observer rebuilt successfully; three existing compiler warnings and
  the duplicate-static-library linker warning remain.
- Full Python suite: **2,390 passed, 1 existing skip, 384 existing numerical
  warnings**, using `/usr/bin/python3 -m pytest -q`.
- Configured Release CTest: **400/400 passed**. Existing unrelated native targets
  are cached, so this is not a clean full-repository rebuild or Linux release CI
  attestation.
- Frozen champion economics files have no diff. Shell syntax and diff whitespace
  checks pass. No test was relaxed to manufacture acceptance.

## Offline use

Provide copied/sealed native segments in causal order, oldest first; do not sort
numeric rotation suffixes lexicographically. The current tape, if included, must
be a stable copied snapshot with complete rows. Preserve `native_generations`.

```sh
python3 scripts/v7_exact_arb_native_evidence.py \
  --segments /evidence/segment-0.jsonl /evidence/segment-1.jsonl \
  --bundles /evidence/native_generations \
  --model-sha EXACT_OBSERVER_MODEL_SHA \
  --output /evidence/analysis
```

The command prints the immutable report path only on success. A prior successful
report is historical evidence, never a substitute for a failed new analysis.
v1 tapes are rejected because they cannot prove sequence/continuity.

## Remaining highest-value work

1. Attach full evidence, causal arrival states and generic N-leg execution to
   native episode identities; incorporate admission, partial fill, unwind and
   resource reservations before computing economic opportunity counts/PnL.
2. Add terminal status/rotation receipts, bounded incremental hourly reduction,
   report retention/offload, full causal replay and restart parity. The reducer
   currently replays sealed evidence, not the raw WS tape into identical orders.
3. Complete global sizing/parity and benchmark the actual champion with observer
   and writer load. Do not remove the legacy Python graph path before parity.
4. Independently attest broader relation semantics, correct coverage/sampling
   weights, calibrate maker risk, and complete the four automatically updated
   requested research documents and the full release gates.
5. Use the official exact-SHA deployment pipeline, establish actual London health
   and run the bounded causal economic window. No new London health or profit
   claim is made here; prior SSH authentication failure is not economic evidence.

`research_decision = null`; the full research goal remains active.
