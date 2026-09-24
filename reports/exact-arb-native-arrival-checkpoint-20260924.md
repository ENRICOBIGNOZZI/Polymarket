# Continuous native WS replay and arrival history checkpoint

Branch: `research/unified-exact-arb-graph`.
Base SHA: `f50147e18e02ebed76cabe900b708e4d9479e603` plus uncommitted changes.
Engineering evidence only. No merge, official artifact, deployment or real order.
This is a manual checkpoint, not one of the four required automatic reports.

## Implemented boundary

`public raw WS frames → same native MarketWsShard decoder → final frame books
→ bounded offline history → exact candidate/frame join → generic N-leg scenario`.

- The research observer records raw frames even without an active graph or a
  positive evaluation. Feed-side sequence assignment precedes queue admission.
  A preallocated 16-MiB byte ring and 4,096 descriptors bound capture; oversized,
  queue/buffer loss and disk suppression have explicit counters. Serialization
  and hashing stay on the writer. This is not a whole-feed allocation claim.
- Session manifests bind model/session/token handles and ticks. Native replay
  checks payload/manifest hashes, frame sequence, epoch, timestamps and decoder
  output counts. It emits final affected books after each complete frame, not
  intermediate states inside a frame. Availability is decode completion time.
- Native replay preserves source version counters where reconstructible. Lost
  frames permanently remove source-counter comparability for that session;
  fresh snapshots cannot invent missing counter increments.
- `v7_exact_arb_native_arrival.py` stores bounded frame/book history in SQLite
  (default 4,096 frames / 64 MiB serialized input budget). Whole-frame eviction
  yields missing history; it never carries an evicted quote forward. The budget
  is a retained-data limit, not a claim of exact process RSS or SQLite file size.
- Candidate construction now requires producer frame sequence and archived
  session-manifest digest. The view joins exact frame, decision time, epoch and
  source-comparable versions, then compares complete normalized decision books.
  Native and replay lineage namespaces are linked only after this check.
- Arrival lookup uses exact monotonic rational time, never rounded wall time.
  Reset/invalid state invalidates untouched token books too. A subsequently
  observed feed gap censors the entire preceding uncertain interval, since the
  missing updates' availability times are unknown.
- Histories cannot supply simulation books until a successful native replay
  receipt matches the exact output hash chain and frame/loss counts. Malformed
  input poisons the history. CLI partial output without the receipt cannot be
  treated as a completed replay. Producer-tail completeness remains unverified.
- Native scenario IDs now bind arrival-tape evidence as well as decision inputs.
  Alternative arrival tapes cannot silently overwrite one another under the
  same cycle ID. Fractional latency arms use exact JSON-safe rational values.

## Validation

- Cross-language integration sends synthetic raw frames through the actual C++
  replay executable, validates its receipt, joins a pinned native candidate and
  runs the generic N-leg simulator. An unfavorable update and a recovery occur
  within the same millisecond: the earlier arrival gets no fills; the later
  arrival can fill. Neither is live or economically verified evidence.
- Tests cover same-frame atomicity, byte-ring wrap with concurrent producer and
  writer, malformed frames, reconnects, missing frames, version comparability,
  duplicate input, payload tampering, clock inversion, truncated CLI input,
  candidate/book mismatch, bounded eviction, poisoned receipts and immutable
  lookup results. Normal capture allocation instrumentation covers `on_frame`,
  not decoder JSON buffer allocation or the whole service.
- Full Python run: **2,442 passed, 1 existing skip, 384 existing warnings**.
- Configured Release CTest: **405/405 passed**. Unchanged targets used cached
  binaries; this is not a clean full Linux release/security gate.
- Loader, WS capture/replay, Python bundle and native-arrival integration:
  **4/4 each in Release, Debug, ASan/UBSan and ThreadSanitizer**.
- The final Python-only preceding-gap censoring guard was additionally tested
  with **52 scoped Python tests** and **2/2 Release arrival CTests** after those
  broad runs. No native implementation changed after sanitizer validation.
- `git diff --check` passes. Frozen champion lane, economics and multi-engine
  files have no diff. No checks were weakened for acceptance.

## Not completed by this checkpoint

1. A production/offline runner that schedules all native episode scenarios over
   bounded windows, persists economic evidence and produces hourly reports.
   The new history is an integration API, not that completed orchestration.
2. Producer terminal receipts, retention/offload and disk-throughput budgeting;
   actual champion before/after load benchmarks and London CPU isolation.
3. Verified current venue timing, ACK/cancellation behavior and fee fragmentation;
   resource/inventory reservations and capital-time economics. Scenario output
   remains `TRANSPORT_SCENARIO_NOT_VERIFIED_VENUE_EXECUTION`.
4. Global sizing optimality/full champion parity, independent semantic breadth,
   coverage-weighted inference, maker queue calibration and the four automatic
   research deliverables; official release/deployment gates.
5. The bounded causal London economic study and final stopping decision.

A fresh read-only `ssh polymarket` service check again returned authentication
failure for `enrico@100.104.183.109`. It established no runtime health, deployed
SHA or profitability fact. This external access issue does not prevent the
remaining local engineering work. No credentials or authority were changed.

`research_decision = null`; the full research objective remains incomplete.
