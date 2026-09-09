# Permanent economic evidence acceptance audit

User specification: all 20 phases in the 9 September 2026 permanent decision-intelligence request. PAPER-only; no capital, sizing, interval or execution-authority changes.

Completion remains unproven until every row has current authoritative evidence. Tests alone do not prove live operation or economic conclusions.

| Requirement | Planned authoritative proof | State |
|---|---|---|
| 1 Source audit and complete catalog | Catalog schema + every live/durable/archive file inventoried; missing source semantics explicit | IN PROGRESS |
| 2 Permanent evidence identity | Versioned source/observation contracts; distinct execution/settlement/policy/protocol identities | PENDING |
| 3 Model-switch invariant | A→cutover→B integration; both original generations reconstruct byte-for-byte | PENDING |
| 4 Profit-cause dataset | Every canonical closed position reconciled; same-decision joins only | PENDING |
| 5 Permanent funnel | Distinct replay keys and attempts; all stages, outcomes and exclusions | PENDING |
| 6 Maker observability | Frozen execution windows; separate markouts; bounded WAITING state; actual censor audit | PENDING |
| 7 Automatic model/protocol cohorts | Live model switch/restart creates new cohort; existing data/report strata retained | PENDING |
| 8 Continuous signal analysis | Fixed cells and regimes, Brier/log-loss/calibration/net returns chronological | PENDING |
| 9 Latency economics | Fixed probability; all five delays, adjacent deltas, quantiles and regime strata | PENDING |
| 10 Maker profit causes | Fill/flow/queue/markout/final analyses with identification limits | PENDING |
| 11 Controlled Maker research | Four paired arms; unchanged pessimistic engine and common cap | PENDING |
| 12 Reproducible ML datasets | Four versioned datasets + source/feature hashes + deterministic regeneration | PENDING |
| 13 Offline benchmark | PM/rich/structural/hybrid/residual; grouped nested chronological audit; no promotion | PENDING |
| 14 Inference | Temporal contract blocks; resolved tails; primary future endpoints frozen | PENDING |
| 15 Quality scorecard | Counts/fractions/contracts/notional/first/last/trend; explicit unknown denominators | PENDING |
| 16 Storage survival | Verified immutable compression; no unique-source deletion; 7/30/90d capacity | PENDING |
| 17 Cutover/restart | Flat/spool proof; source manifests; no duplicate or old-SHA execution owners | PENDING |
| 18 Remote health | Tailscale inventory/job evidence; working route or precise external blocker | PENDING |
| 19 Automatic decision report | Live canonical evidence → ranked, qualified diagnosis | PENDING |
| 20 Next economic action | NEXT_ECONOMIC_ACTION.md answering all eleven questions with evidence | PENDING |
| Engineering validation | Release, Debug, ASAN/UBSAN, Python, provenance, single-writer, relevant latency gates | PENDING |
| Final live acceptance | Exact-SHA CI/deploy and actual permanent collection across cutover | PENDING |

Baseline: primary live checkout `a1ff491559aeb7fc45ff077962fc0b3806133684`; isolated implementation worktree `codex/permanent-economic-evidence-20260909`. Initial live inventory is stored outside git under `runs/permanent_evidence_20260909` in the primary checkout.

## Updated user storage constraint

The user explicitly capped **all data at 30,000,000,000 bytes** and rejected external storage. This supersedes the earlier tiering proposal. Collection backfill session 60202 was stopped before starting lossless compaction. Current consumption exceeds the requested cap; this is a migration in progress, not a claim of compliance.

The first real closed 67.1 MB native book segment was migrated into a transparently compressed immutable pack. Both original hardlink aliases still read the identical SHA-256. The permanent store can reconstruct its prior chunk revisions through verified pack slices, allowing redundant gzip objects to be removed without losing a unique byte. Initial measured savings: 62,013,440 source bytes plus 4,067,328 redundant object bytes. Proof: `runs/permanent_evidence_20260909/lossless-pack-first.json`.

No loss of unique historical information has been authorized or implemented. Long-run feasibility under the fixed cap must be measured after deduplication; irreversible aggregation must not silently masquerade as lossless compression.

## Implementation progress (not deployment acceptance)

- Exact embedded Taker coordinator receipts now join namespaced replay keys. Attempts, orders, fills and positions are distinct. Canonical 23 Maker fills came from 21 orders; this was not missing data.
- Four dataset view builders regenerate deterministically from immutable revisions after producer files and the index are removed. Initial live materialization: 90 settlement rows, 1,185 Maker orders and 42 economic positions, all 42 cash positions reconciled, PnL −9.92742799000000195. PM-response source was outside that initial two-source view; all-history incremental materialization remains pending.
- Signal reports now expose fixed marginal regimes, Brier/log loss/calibration, cost-stressed settlement returns, delay levels and adjacent deltas, and chronological folds. Historical added regimes are marked exploratory. Both existing protocol generations remain in separate report strata.
- Extreme Bonferroni tails are suppressed when Monte Carlo resolution is inadequate. Temporal moving-block sensitivity and three prospectively frozen future endpoints are implemented. Final confirmatory locking and live forward registration remain pending.
- Focused checks: 49 profit tests, 7 permanent-evidence/dataset tests, 3 Maker-window tests, 3 lossless-compaction tests, and standalone coordinator integration scripts pass. Full engineering gates and live deployment remain pending.
- Remote health blocker is documented in `docs/V7_REMOTE_HEALTH_BLOCKER.md`.

## Latest authorized cleanup and compression progress

The user explicitly requested deletion of GitHub Tailscale connections. A fresh local read confirms 997 offline, expired `github-runnervm…` peers; all exact IDs are frozen in the authorized cleanup scope. `v7_tailscale_ci_cleanup.py` implements administrative revalidation, restricted DELETEs and final inventory verification. Two tests pass. Actual deletions remain pending an administrative API credential; the user was asked for a local token-file path, never a secret pasted in chat. Deploy now has the same proposed ephemeral-authkey fallback guard as health/archive. No workflow change has been deployed.

The all-runs compaction batch completed 1,000 groups, zero deferred, with 46,184,103,936 estimated bytes reclaimed (sum of verified source/object transformations). This is not a claim that the global 30 GB limit has been met. Recompression of earlier large packs is running separately using Apple Archive; old byte hashes, shared aliases and CAS references are preserved. A new fourth compaction test proves alias/CAS survival through re-encoding and rejects uninspected links. Pack transforms now retain the exact loaded implementation source hash and immutable code bytes.

Runtime status still reports running at a1ff491559aeb7fc45ff077962fc0b3806133684, PAPER-only, authenticated execution false, real submission false. Full process and deployment acceptance remains pending.

## Verified archival resume and expanded offline benchmark

- After a compression-induced inode change, append capture now compares the entire previously preserved prefix before reusing the old revision chain. Matching tail/size alone cannot authorize reuse. Tests preserve prior revisions after replacement and reject changed interior bytes with an identical tail.
- Retention recognizes read-only aliases owned by checksum-verified shared-pack manifests; it leaves them in place and still rejects uninspected hardlinks. Nine retention tests pass.
- The permanent collector excludes its entire derived subtree and defers additional archive copies when all-runs usage plus reserved headroom reaches 30 GB. This copy guard does not attest a hard cap on producers; continuous producer segmentation and the final global cap remain unfinished.
- The existing economic observer loop now includes bounded, nonblocking compaction and budget-guarded collection; no child process or execution owner was added. The process manifest validates 21 launcher children, 23 declared processes and exactly one owner for each authority. All historical profit cohorts are read in separate strata. These launcher changes are not deployed yet.
- Expanded dataset: 2,593 settlement rows over 557 contracts, using the initial current-run sources plus the preserved 671,088,640-byte durable counterfactual prefix. This is explicitly incomplete historical coverage. Manifest `0e04a1284260a3d8e9303cadc33ed46ec8b72f6403ae888078aac7e01d6264e5`; 42 canonical positions still reconcile to −9.92742799000000195 with zero residual.
- Offline benchmark `303ddb47e8135c5e14df6109f9ef1213b4ec003005681e471b6981beecb54536`: three expanding inner contract folds, label-availability embargo, regularization 0.1/1/10. Their Brier improvements over paired PM were −0.00244046/−0.00206637/−0.00081161, so the frozen reference remains PM. The final untouched audit has 112 contracts, PM Brier 0.13748904. Structural and hybrid recorded predictions have paired improvements −0.05979962 and −0.01427673. Rich predictions cover only 43 audit contracts and have paired improvement −0.00089299. Missing predictions remain explicit; these are not profitability or causal claims. Exact implementation source bytes are preserved beside the benchmark.
- The latest separate live attribution has 43 closed positions, −11.07742799000000205 canonical PnL, and zero unexplained residual. This newer ledger prefix is distinct from the frozen benchmark dataset.
- Focused permanent suites now have 13 passing tests; profit suites 49; compaction 4; retention 9; process-manifest standalone validation passes. Full Release build/test is running; Debug, sanitizers, exact-SHA CI and deployment remain pending.

## Release check and live budget guard

Release compilation and all 138 configured CTest entries passed. The internal synthetic latency gate also passed: receive-to-intent p99 14,917 ns and p99.9 104,166 ns; this does not measure network or CLOB latency. Three additional capacity tests verify hardlink/overlapping-root accounting, immediate cap alerts without throughput data, and exclusion of repeated hashes/initial backlog from production rate.

The copy guard was exercised against the real data tree: 46,551,851,008 allocated bytes including directories, zero permitted copy budget, `ARCHIVE_COPY_DEFERRED_DATA_BUDGET`. It wrote the observational status and did not create additional source copies. Compaction continues; total producer storage is still above the user's 30 GB ceiling. The exporter now exposes this status through `/permanent-evidence.json` in the proposed code. Debug and ASan/UBSan builds are running; macOS sanitizer invocation disables unsupported leak detection, while Linux exact-SHA CI remains required.

Debug and ASan/UBSan also completed with all 138 CTest entries passing. The 15 targeted monitoring/permanent/retention entries passed again after the exporter and storage changes. These are local worktree checks, not exact-SHA CI or deployment proof.

## Public origins, bounded journals and current migration status

- Safe closed-tape compression fourth receipt: 342 segments, 10 skipped, zero failures, 7,891,953,193 bytes reclaimed. The subsequent all-runs inode-deduplicated file allocation scan measured **39,239,397,376 bytes**, excluding directory blocks. The 30,000,000,000-byte global cap remains unmet. A third shared-pack compaction pass is running; it does not touch open producer streams.
- A model-independent public input cut is now collected even without a valid fitted probability model. Each origin is persisted once; four horizon labels reference its exact immutable hash. The training reader, forward audit and permanent dataset builder support linked and legacy rows. Dataset tests reconstruct both label and raw origin after producer files and the index are removed.
- Lead/lag production now has proposed 64 MiB hot-segment rotation with one background compression worker, decoded byte verification and compression receipts before redundant plain-copy removal. Tests cover restart, an active read during rotation, corrupted archive conflicts and fail-closed compression backlog. Other large mutable producer streams still need bounded storage; the overall producer budget is not yet enforced.
- Hybrid identity was corrected to include the external model hash and actual blend recipe. Router identity survives pending-forecast reconstruction across code changes; recorded probabilities are unchanged. Missing historical research hashes caused by the field-name mismatch are now read from the exact source keys.
- The automatic decision report and generated memo currently cover the verified 43-position canonical prefix, PnL −11.07742799000000205, zero unexplained residual. This is not all-history acceptance. Arrival probabilities, several event-level data-quality exposures, complete Maker-window proof and final confirmatory locks remain unfinished.
- Zstandard sample: 67,120,804 decoded bytes, original gzip 5,071,856 bytes. Level 9 produced 3,437,519 bytes in 1.306 seconds with verified roundtrip; level 15 produced 3,385,751 in 2.972 seconds. This is one native book segment, not a steady-state or 90-day capacity proof. No historical representation was changed by this experiment.
- After these changes, Release was rebuilt and all **141 CTest entries passed**. Earlier Debug/ASan runs covered 138 entries; final exact-SHA CI, canonical flat/spool proof, deployment and all live acceptance items remain pending.
- Tailscale: the authorized 997-node scope is unchanged. No administrative token-file path has been supplied and no device has been deleted.

## Reviewable checkpoint and native window proof

Draft PR #896 contains checkpoint `5d99710aaf5a590ade9267b9f56e42b46e79e97b`. Its PR-triggered `ci`, `monitoring` and `Private runtime single-writer validation` workflows passed. This is branch-checkpoint validation, not final-main-SHA deployment acceptance.

The subsequent Maker-window change requires both the consumed native monotonic watermark and the published monotonic watermark to cover the requested execution/markout target. BookTimeline invalidates a regressing monotonic clock. A native replay integration test now freezes operational results at 5.1 and 10.1 seconds, restores them from emitted records, introduces a later transport gap, and verifies that only later markouts are censored. The ten relevant local CTest entries pass, including the native replay test. Live forward effectiveness and complete event-level censor accounting remain unverified.

## Final-look closure and additional bounded producers

- Checkpoint `02951431d30879738b124e21e0f523d7a314e2f3` passed PR CI, monitoring and private runtime single-writer validation.
- The fixed-window final now requires a producer closure after pending window observations drain, complete terminal labels and verified public settlement records. A checksummed immutable final preserves its exact contract inputs and implementation source. Primary censoring suppresses confirmatory intervals. Later source changes are flagged and cannot replace the first look. Offline reports do not publish a final. Five dedicated tests cover missing data, producer closure, terminal censoring, late inputs and corruption.
- Coordinator result publications, pinned Maker selector publications and Binance/Deribit/Coinbase REST observations now use the bounded compressed journal in the proposed launcher/code. A lifetime writer lock rejects a second journal producer. Legacy reads are read-only; gzip publication, raw removal and crash recovery retain verified source bytes. Durable fair counterfactuals and remaining native legacy streams still need bounded storage.
- All 143 configured CTest entries passed in Release, Debug and ASan/UBSan. macOS sanitizer testing used `detect_leaks=0`; this is not a leak-check claim. Subsequent focused tests passed for read-only journal recovery, fixed-region ranking and canonical report-total consistency.
- A journal-only measurement over 512 consecutive real coordinator reports had p99 append-plus-fsync 351,417 ns and maximum 499,916 ns, with exact record roundtrip. This does not measure coordinator execution or network latency. Artifact: `coordinator-journal-latency.json`.
- Third compaction completed 2,226 groups, zero deferred, recovering 822,386,688 bytes. Completing the previously interrupted pack recovered another 409,980,928 bytes. Fifth safe retention compressed 98 segments, zero failures, recovering 2,569,792,692 bytes. The resulting all-runs allocation including directories was **37,803,466,752 bytes**, still above the 30 GB cap; ongoing old-runtime collection continues to grow it.
- Fresh attribution still has 43 closed positions and canonical PnL −11.07742799000000205, all reconciled. The primary signal cohort has 183 selected contracts, 182 resolved, descriptive Brier improvement over PM −0.0006772523 and frozen-signal 0→1000 ms margin change −0.0008112507 per share. Neither existing historical cohort has prospectively registered primary endpoints; no retrospective confirmatory final was manufactured.
- The latest generated diagnosis includes strongest/weakest fixed regions with a minimum 12-contract display filter and explicit exploratory ranking status. Internally inconsistent canonical totals are rejected. This remains a current-run/current-cohort analysis, not full-history completion.
- The review remains a draft. No new PAPER deployment, main merge or Tailscale deletion has occurred. Global 30 GB enforcement, full historical coverage, remaining quality exposures and final live cutover acceptance remain unfinished.
- Follow-up verification: all three PR workflows passed for exact commit `e306dff6867f1bf7a256b0c9ad4262b99dd58128`. No deployment followed.
- Frozen Maker execution now has its own descriptive report population even when the final comparison is missing. Missing markout labels remain `NOT_RECORDED`; later transport censors do not remove the execution. Exact duplicate windows count once, missing anchors are exposed, and a completed comparison that changes frozen execution is rejected. This population never supplies confirmatory endpoints. All 63 `test_v7_profit*.py` tests passed; native replay/report integration passed with Release, Debug and ASan/UBSan binaries (4 tests each). Historical live cohorts still contain no frozen-window records; no unavailable historical result was reconstructed.
- Sixth closed-segment compression archived 103 segments with zero failures, recovering 2,853,534,277 bytes. A subsequent scan measured **38,485,184,512 allocated bytes including directories**, still above the 30 GB ceiling. Artifacts: `safe-retention-sixth.json`, `storage-after-sixth.json`, and `profit-frozen-execution-audit.json` under the existing live `runs/permanent_evidence_20260909` audit directory. Ongoing old-runtime collection remains unbounded in several streams; this is not sustainable-capacity acceptance.
