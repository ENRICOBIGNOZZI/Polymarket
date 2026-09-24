# Native WS integration checkpoint — engineering, not economic completion

Branch: `research/unified-exact-arb-graph`.
Base SHA: `f50147e18e02ebed76cabe900b708e4d9479e603` plus uncommitted changes.
This supersedes the loader/WS integration status in the preceding native-runtime
checkpoint. No exact-SHA deployment, merge, execution-authority change or real
order occurred. The full research goal remains active.

## New implemented path

`Python proof compiler / bounded hotset → immutable payload + source lease →
off-path native proof loader → causal WS frame end → native affected relations →
bounded telemetry/full-book evidence → control-thread JSON writer`.

- `v7_exact_arb_graph_loader`: verifies PAPER/model identity, exact serialized
  payload digest, source expiration, token binding, fee/minimum/tick consistency,
  market windows, dependency completeness and duplicate portfolios. It recomputes
  every statewise payoff with arbitrary-precision rational arithmetic. The
  supplied finite-state theorem is not independent settlement attestation.
- The loader enforces the existing 5-bps guarantee-scaled reserve floor. Unknown
  fees, unsupported terms, expired sources and mismatched ticks fail closed.
- `v7_exact_arb_graph_shadow`: consumes actual decoder snapshots after the full
  WS frame has been decoded, with one evaluation per affected relation. Generation
  payloads and source selections are archived before publication. Observations
  carry both hashes, source deadlines, epoch/frame, leg versions, integer economic
  outputs and decision timestamps. Accepted evaluations copy every required leg's
  full depth into a bounded queue; no writer retains hot-thread references.
- Feed evaluation performs no JSON, file access or unbounded queueing. Queue
  saturation drops research telemetry explicitly. Disk suppression has separate
  counters. Both evidence tapes rotate at 64 MiB; drain passes are bounded so a
  busy producer cannot indefinitely starve source refresh.
- The dedicated graph observer receives `--graph-native-shadow` and the existing
  read-only capital policy through launcher/manifest. The flag cannot be combined
  with champion PAPER economics or a non-selection observer. SELL inventory is
  empty; no synthetic prefunding or shorting is introduced.
- Source failures invalidate native decisions without terminating the dedicated
  observer. It can continue collecting WS data and recover on a valid source.
  Corrupt frames invalidate cached lineage. Observer destruction joins the feed
  before native state can be reclaimed.
- Canonical health now uses the native graph status. A healthy legacy Python
  shadow cannot mask a missing/degraded native worker. Exporter fields distinguish
  frames, relation evaluations and dropped telemetry records; a research-only
  alert covers unhealthy native evidence. Champion health stays independent.

## Evidence and verification

- Full Python suite: **2,366 passed, 1 existing skip, 384 existing numerical
  warnings**, using `/usr/bin/python3 -m pytest -q` (Python 3.9.6).
- Configured Release CTest: **398/398 passed**. Changed native targets rebuilt;
  unrelated targets used the existing configured build tree.
- Runtime, loader/WS adapter and Python-to-C++ roundtrip: **3/3 each** in Release,
  Debug, combined ASan/UBSan and a new separately configured ThreadSanitizer build.
- End-to-end fixtures exercise the real decoder, full leg evidence, 5,000
  undrained frames, explicit queue drops, disk suppression and source expiry.
- Cross-language roundtrip produces the same decision-output hash three times.
  Altered payout vectors remain rejected after a freshly calculated payload hash.
- Initial roundtrip fixture accidentally used the exact market-close timestamp;
  the loader correctly rejected it. The fixture now derives a noon UTC timestamp
  within its declared market window; the deadline guard was not relaxed.
- Release observer builds successfully with three pre-existing compile warnings
  and a duplicate-static-library linker warning. Scoped observer/manifest/health
  tests were rerun after the final source-outage recovery edit.
- Frozen `v7_pure_arb_lane.hpp`, `v7_pure_arb_economics.py` and
  `v7_pure_arb_multi_engine.cpp` have no diff. `git diff --check` and launcher shell
  syntax checks pass. New Linux CI workflow coverage is configured, not executed
  or release-attested in this session.

## Not yet complete — do not reinterpret these observations as profits

1. The legacy Python graph traversal still runs alongside native observations.
   Native near-arb/funnel/lifetime parity, durable episode deduplication and the
   downstream execution bridge must be completed before removing that live path.
2. Native quantities remain mathematical microshare capacity. Exact venue order
   precision, capital/inventory reservations and executable N-leg admission still
   require completion. Outputs deliberately say `actionable=false`,
   `economic_execution_verified=false`, and
   `CAUSAL_FRAME_END_PRE_ALLOCATION_EVALUATION`. Opportunity count and execution
   PnL in native health are null, never sums of repeated positive updates.
3. Independent NegRisk/other semantic attestations, broader verified relation
   families, full arrival/unwind/capital-time analysis and maker evidence remain
   open, as do the four requested automatically updated research documents.
4. Whole-tape replay across crash/restart, full champion parity, native throughput
   and writer-load benchmarks, retention/offload integration and London before/
   after isolation measurements are still required. Synthetic stress success
   does not establish safe deployed resource use.
5. Complete release gates, official deployment and the bounded 8–48-hour causal
   economic study remain outstanding. The last read-only London check failed SSH
   authentication; that is not evidence of service failure or negative economics.

`research_decision = null`. Next highest-value work: native diagnostics and a
reconstructible episode/execution bridge, completing venue/resource admission,
then removing per-update Python graph traversal with parity evidence.
