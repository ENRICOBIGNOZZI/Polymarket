# Hourly native-study attribution — not a live reporting service

Branch `research/unified-exact-arb-graph`, base HEAD
`f50147e18e02ebed76cabe900b708e4d9479e603` plus local uncommitted changes.
PAPER / SHADOW only. No merge, deployment, orders or authority changes.

## New executable path

The existing `v7_exact_arb_native_run.py` now publishes `hourly.jsonl` with each
successful immutable study. Its SHA256 is in `report.json`; the hourly reducer's
source hash participates in study identity. Repeat input reproduces the output;
a corrupt published artifact is rejected, not silently replaced. Publication
still occurs only after the complete supplied study passes validation.

`v7_exact_arb_hourly.py` groups observations into half-open 3,600-second windows
of the recorded **session host monotonic clock**. These are not UTC hours and
their span does not prove continuous service uptime or causal market coverage.
Session identity remains mandatory: hour indices from different sessions must
not be combined as if they shared a clock or an independent opportunity set.

The whole-session `Evidence` reducer now returns validated, deduplicated funnel
increments. Hourly reporting consumes those same increments instead of running
a second episode detector or resetting state at each boundary. Thus a positive
episode continuing into another hour contributes more evaluations, not another
opportunity arrival. Gaps, lineage resets, incomplete sizing and left censoring
retain their existing treatment. Hourly totals reconcile to whole-session counts.

Each receipt contains:

- Recorded frames/evaluations, invalid frames and sequence gaps **when detected**.
  These are not inferred producer-drop counters or the unknown time of each loss.
- Family/direction funnel and distinct observed economic relations.
- Near-arbitrage PUSD/share, ticks and bps quantiles, plus fractions within
  0.25/0.5/1/2/5 ticks. Values and ordering use exact rational arithmetic, including
  distances beyond binary64's exact integer range. These remain recorded-model,
  top-of-book diagnostics, not executable capacity or verified venue fees.
- Paired host-stage latency quantiles, including p99.9 and max. Missing raw-frame
  joins do not manufacture zero latency. The existing whole-study latency report
  also now includes p99.9. Stage definitions remain unchanged.
- Distinct capital/latency world's result counts, known flat model unwind PnL
  and reservation-time quantities for closed groups. Full acquired inventories
  do not become realized PnL. Alternative worlds are never added together.
- Recorded wall-clock ranges/backward steps, explicitly without UTC attestation.
- Unknown runtime health, exchange coverage, conservative portfolio PnL,
  economic rates and research decision as `null`, not zero.

Diagnostic observations are attributed at their recorded emission time.
Execution outcomes are attributed when known to the model. These are different
populations: dividing a later hour's fills by that hour's new opportunities
would be an invalid conversion rate. Reservation-time sums belong to completed
groups and are not a prorated hourly occupancy curve.

## Causal correction found during integration

When replay ends before a pending modeled ACK, the shared execution queue must
censor the outcome. Previously its capital receipt could carry the unobserved
scheduled future timestamp even though no funds were released. Such a diagnosis
is now recorded at the last observed replay watermark, not the future ACK.
Its holding duration remains unknown and its reservation remains encumbered.
The hourly reducer rejects future or pre-capture capital results.

## Bounds and adversarial checks

The reducer uses the runner's bounded-input temporary SQLite storage, not the
native hot loop. It caps the span at 4,096 hourly windows and 128 family/direction
groups per window, sorts exact distances on disk and emits one bounded window
at a time. Existing input-byte/row, scratch-space and scenario-world limits apply.
No producer tail completeness, independence or polling/scheduling guarantee is
inferred from a successfully sealed input replay.

Tests cover cross-hour episodes, emission vs decision-time boundaries, entirely
empty intervening hours, gaps attributed when detected, duplicate inputs,
invalid diagnostics/frames, exact large rational distances, missing raw frames,
backward wall clocks, reversed emission clocks, explicit span bounds, separated
worlds, future/pre-capture outcomes, counter reconciliation, deterministic runner
publication and corrupt artifacts. The existing warning that a live hourly
multi-session service is not established remains tested and preserved.

## Measured validation

- Full local Python suite: **2,767 passed**, one existing skip, 384 numerical warnings.
- Full configured Release CTest after registering the new module: **416/416 passed**.
- Native decoder → Python study/CLI integration passes in Release, Debug,
  combined ASan/UBSan and TSan. Native code was unchanged in this increment;
  cached native builds were used. This does not sanitize Python or certify Linux CI.
- No diff in frozen champion lane, economics or multi-engine files.
- No checks were removed or weakened. `git diff --check` passes.

## Remaining operational and economic work

This closes per-study hourly partitioning, **not** a live timer, a safe
multi-session/prefix aggregator, session-bound historical health or weighted
exchange-wide coverage. Those need explicit input manifests, overlap/restart
handling and the official deployed runtime. Model fee semantics, independent
NegRisk attestations, maker calibration, real capacity/settlement, worst-case
load, champion latency non-regression, official gates and London exposure remain
open as described in the preceding checkpoints.

No economic stopping decision is supported yet. `research_decision = null`.
