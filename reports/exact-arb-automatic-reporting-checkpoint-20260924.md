# Automatic study reporting checkpoint

Historical checkpoint. Subsequent [hourly study attribution](exact-arb-hourly-attribution-checkpoint-20260924.md)
adds `hourly.jsonl`; it does not establish a live hourly multi-session service.

Branch: `research/unified-exact-arb-graph`.
Base SHA: `f50147e18e02ebed76cabe900b708e4d9479e603` plus uncommitted changes.
PAPER only, zero authority. No release, merge, deployment or profitability claim.

## Automatically generated deliverables

`scripts/v7_exact_arb_native_run.py` now generates the following on every
successful immutable study publication, using `v7_exact_arb_sota_report.py`:

- `SOTA_GAP_ANALYSIS.md`: current evidence, target, limitation and next action.
- `ECONOMIC_FUNNEL.md`: family/direction diagnostic counts, censored episodes,
  near-arbitrage quantiles and distinct independent/shared simulation worlds.
- `LATENCY_ATTRIBUTION.md`: measured host-clock stages and explicit missing stages.
- `SOTA_CHECKPOINT.md`: study/model/source identities, configuration, optional
  canonical health snapshot, data quality and unresolved gates.

They live beside `report.json` and the study's JSONL evidence, not in a mutable
repository-wide "latest" document. Their hashes are recorded in `report.json`.
Publication uses the runner's existing atomic immutable-directory mechanism;
repeated input is reproducible, and corruption of an existing document fails
rather than silently reusing or overwriting it. Reporter source hashes are part
of the study identity. This checkpoint itself is a manual engineering record,
not one of the automatically generated study documents.

## Measured latency, without mislabeled compute

Native diagnostic rows join to the fully validated native raw-frame replay by
session, frame sequence, epoch and exact receive/graph-start timestamps. Missing
raw frames censor the measurement. Contradictory clocks or an observation on an
invalid source frame fail publication. Diagnostic duplicate rows are counted
once. Samples and exact order statistics are stored/computed on bounded-input
SQLite scratch storage, not on the native feed thread.

Measured intervals are receive-to-decode, decode-to-graph-start,
graph-start-to-relation-emission and receive-to-relation-emission. Quantiles are
p10/p50/p90/p95/p99/max. Contributions use sums of paired stage durations over
the same observations, not sums of marginal quantiles. Integer totals avoid
SQLite signed-64 SUM overflow. An empty or all-zero-duration population cannot
manufacture latency percentages.

Graph start is frame-wide: later relation emissions include earlier evaluations
and telemetry copying. This is **not isolated relation compute latency**.
Decode time also includes book processing. Samples are relation-update-weighted;
multiple relations share a frame's decode interval. External wire, OMS/risk,
serialization, exchange ACK/fill and isolated detector times remain unmeasured.
Hypothetical arrival-delay arms are never presented as London measurements.

## Optional canonical runtime health

The runner accepts these three inputs together:

```text
--runtime-health /path/to/canonical_runtime_health.json
--runtime-target /path/to/deploy/london/runtime_identity.json
--report-as-of-ms <explicit-frozen-assessment-time>
```

Both original inputs are archived and hashed with the study. No historical
instance fallback or automatic deployment-SHA inference exists. Missing optional
inputs remain unknown; an explicitly requested missing file fails rather than
reusing a prior success. Unsafe/malformed authority contracts fail closed.
Stale/future receipts, wrong instance/AZ and model/release SHA mismatch suppress
eligible health display. Receipt freshness is capped at five seconds.

Even a fresh matching receipt describes only its collection instant and its
producer's reported checks. It is not an independent remote attestation, evidence
of uninterrupted study uptime, or a promise that a source lease remains valid at
the later report-as-of. Health never admits trades or changes the economic result.

## Economic interpretation

Readiness counts overlap and are not falsely labeled as sequential conversion
rates. Top-of-book diagnostics remain separate from full-depth simulated fills.
Alternative capital/latency worlds are never pooled as profit. No PnL rate,
capacity curve, sampling coverage, causal root cause or STOP/CONTINUE decision is
manufactured. The pre-existing general economic accounting reporter is not used
as an exact-arbitrage stopping-rule evaluator: its accounting population differs.

## Validation and remaining gates

Final full Python suite: **2,529 passed, 1 existing skip, 384 existing numerical
warnings**, using `/usr/bin/python3 -m pytest -q --disable-warnings`.
After CMake reconfiguration to register the new test module, **409/409 configured
Release CTest tests passed**. The native decoder → study runner/CLI integration
passed again in Release, Debug, combined ASan/UBSan and ThreadSanitizer. Native
code was unchanged in this reporting increment; cached binaries were used. This
is not a clean Linux release/security gate or a sanitizer attestation of Python.
`git diff --check` passes; frozen champion files have no diff.

New adversarial coverage includes reproducible rendering after JSON key reordering, corrupt
documents, duplicate observations, paired quantiles, oversized integer totals,
missing latency, invalid source frames, receipt expiry/identity/authority and
missing-source failure. The review workflow includes the new test module.

Still required: hourly multi-session orchestration, independently verified
execution/fee/metadata invalidation, capital release and global sizing/parity,
semantic breadth, maker calibration, true champion load/latency comparison,
clean release/security gates and the bounded London economic study. The four
reports expose these gaps; generating them does not close them.

The read-only London probe again returned SSH authentication failure against
`100.104.183.109`. No current service state or deployment identity was inferred.
No frozen champion lane/economics/multi-engine files were changed.

`research_decision = null`; the full objective is still incomplete.
