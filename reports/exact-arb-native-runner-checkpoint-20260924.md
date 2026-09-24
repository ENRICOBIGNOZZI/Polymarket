# Native session study runner checkpoint

Branch: `research/unified-exact-arb-graph`.
Base SHA: `f50147e18e02ebed76cabe900b708e4d9479e603` plus uncommitted changes.
PAPER-only engineering work. No authenticated execution, real order, promotion,
merge, official release artifact or London deployment.

## New executable workflow

`scripts/v7_exact_arb_native_run.py` now joins recorded native observations,
diagnostic episode starts, full candidate evidence and continuous native WS
replay into a content-addressed session study.

1. Validate the complete native replay and its final output-chain receipt before
   exposing any prefix for execution scenarios. Store its exact serialized rows
   and prefix digests in a private temporary disk archive.
2. Replay that verified archive through the existing bounded arrival history.
   A prefix's digest must match the first pass. No forged intermediate receipt
   or mutable `verified=true` input is used to bypass terminal verification.
3. Reduce diagnostic observations with the existing episode reducer. Select only
   known negative-to-positive pre-allocation starts. Future episode duration or
   closure is not a selection feature; initial positives remain left-censored.
4. Require full evidence to match its diagnostic observation exactly, excluding
   only schema and added leg books. Independent queue loss can leave either row
   absent; missing full evidence is a censored admission, not zero PnL.
5. Schedule independent frozen latency/mode/order-policy arms on disk. Evaluate
   each once when replay availability is strictly later than its full horizon,
   including unwind. A later frame exposes gaps and additional same-time updates.
   Expired/evicted history and an insufficient terminal tail censor the scenario.
6. Publish `report.json`, `episodes.jsonl`, `admissions.jsonl` and
   `scenarios.jsonl` together in one immutable content-addressed directory. Reuse
   verifies artifact hashes; corruption is not silently accepted or overwritten.

Default arms are parallel FAK scenarios at 1/2/5/10/25/50/100 ms, with two-ms
counterfactual unwind delay. Custom arms must be supplied as a frozen JSON list;
the runner performs no threshold or latency search. Mode support does not verify
venue batch atomicity or ACK timing.

## Safety and accounting

- Session/model/manifest identity is checked across inputs. All required PAPER
  flags remain explicit. No order, credentials, network or promotion interface
  was added to the runner.
- Duplicate rows do not create additional episodes. Conflicting duplicates,
  native/full-observation mismatches, a bad replay receipt, malformed inputs or
  budget violations prevent publication of a new report.
- History has independent frame and byte retention limits. Input rows/bytes and
  working disk usage are bounded; insufficient free disk fails closed. Temporary
  analysis data is cleaned up, never unrelated evidence or runtime files.
- Study and research-scenario identities bind source hashes, normalized inputs,
  manifest, replay receipt, frozen scenario arms and history/budget configuration.
  These hashes are not substitutes for official deployed-artifact identity.
- Results are counts of independent execution scenarios, not jointly feasible
  trades. The report deliberately leaves portfolio net PnL, opportunity rate,
  capital efficiency, exchange coverage and research decision null. It does not
  sum competing latency arms, overlapping depth, or unreserved capital.
- SELL candidates receive no synthetic inventory; they remain censored until
  verified inventory/resource reservations exist.

## Offline usage

First run the native replay on copied/sealed raw WS segments in chronological
order; preserve the matching archived session manifest and graph bundles. Check
successful native exit and retain its final receipt. Then:

```sh
python3 scripts/v7_exact_arb_native_run.py \
  --model-sha EXACT_RECORDED_MODEL_SHA \
  --session-manifest /evidence/native_sessions/SESSION_HASH.json \
  --replay-output /evidence/native-replayed.jsonl \
  --observations /evidence/observations-0.jsonl /evidence/observations-1.jsonl \
  --full-evidence /evidence/full-0.jsonl /evidence/full-1.jsonl \
  --bundles /evidence/native_generations \
  --output /evidence/studies
```

Each invocation covers one recorded session and frozen configuration. The output
path is printed only after successful immutable publication. This is not yet an
hourly deployed service or multi-session portfolio allocator.

## Verification

- New runner tests cover complete joins, separate arms, deterministic repeated
  publication, duplicate input, missing full evidence, left-censored episodes,
  bad/missing receipts, extra/truncated input, wrong sessions, full-row mismatch,
  retention eviction, input/disk budgets, corrupted prior artifacts, loss before
  arrival, strict terminal lookahead for data completeness and configuration IDs.
- The existing cross-language integration now also invokes this runner and its
  CLI against output from the real native WS decoder. Synthetic fixtures yield
  no fill at 0.2 ms, all legs filled at 1 ms, and censored evidence at 100 ms.
  These are correctness fixtures, not measured London economics.
- Configured Release CTest: **406/406 passed**; unaffected native targets used
  cached binaries. This is not a clean full Linux release/security attestation.
- Cross-language runner/CLI integration passed in Release, Debug, ASan/UBSan and
  ThreadSanitizer. No native implementation changed in this checkpoint.
- Final configuration-ID/constructor cleanup changes additionally passed
  **32 scoped Python tests** and **3/3 Release arrival/runner CTests**.
- Final full Python suite: **2,460 passed, 1 existing skip, 384 existing numerical
  warnings**, using `/usr/bin/python3 -m pytest -q --disable-warnings`.
- Frozen champion lane, economics and multi-engine files have no diff;
  `git diff --check` passes. No tests or authority boundaries were weakened.

## Still required

The four automatically updated SOTA documents, hourly/multi-session orchestration,
resource-constrained portfolio allocation, verified venue timing/fee-fragmentation
and capital lock, full raw-tape graph-decision parity, global sizing optimality,
independent semantic attestations, maker calibration and actual champion load
benchmarks remain open. The runner joins native diagnostic decisions; it does
not independently recompute every graph decision from raw WS input.

Official exact-SHA release/deployment and bounded London causal evidence remain
mandatory before an economic decision. The last read-only London check in the
preceding checkpoint failed authentication; no new live health claim is made.

`research_decision = null`; the full objective remains active and incomplete.
