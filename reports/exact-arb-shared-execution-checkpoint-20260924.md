# Shared event-ordered execution checkpoint

Branch: `research/unified-exact-arb-graph`.
Base SHA: `f50147e18e02ebed76cabe900b708e4d9479e603` plus uncommitted changes.
PAPER-only, zero-authority. No merge, deployment, order or capital authority.

## Correctness defect removed

The prior generic N-leg simulator fetched all arrival books and checked the
terminal watermark before simulating its first leg. Missing later data could
therefore hide an earlier counterfactual fill. It now advances one arrival at a
time, retaining known entry/unwind fills when subsequent evidence is unavailable.
Full fills do not wait for an unwind that is not required. Actual completion time
and the scheduled maximum unwind horizon are separate fields.

The execution model is now `CAUSAL_LIMITED_ORDER_V3_EVENT_ORDERED`. Historical
V2 scenarios must not be silently combined with its results.

## One kernel, two evaluation scopes

`execution_steps` yields each entry or unwind arrival before reading its book or
changing liquidity. The existing independent `simulate` API drives this kernel
to completion. The new `v7_exact_arb_shared_execution.py` schedules the same
kernel across competing episodes in timestamp order; it is not a separate copy
of fee, fill, proof or unwind economics.

- Orders remain pinned to decision-time limits. Every unwind limit is frozen at
  its submission time before any later unwind fill is processed.
- Parallel/batch scenarios do not acquire basket atomicity. Equal-time ordering
  is explicitly canonical opportunity ID then leg, not an attestation of venue
  priority. Sequential legs from different episodes interleave by arrival time.
- Each capital/latency arm has its own shared-depth world. Liquidity and fills
  from different alternative worlds are never combined.
- A censored order can have unknown fills. From that causal instant onward the
  shared world is marked uncertain and later fills are censored, not granted
  against liquidity assumed unconsumed. Earlier completed fills remain evidence.

## Shared liquidity lifecycle

Depth debits are keyed by token, side and exact price, not book version or graph
lineage. An unrelated update, heartbeat or new version therefore cannot restore
consumed shares. Observed size increases expose only the increment above the
remaining debit. Debits clear only when the price is absent from a valid,
continuous, complete side of the public book. Truncated, invalid or crossed
books cannot attest that reset.

The native liquidity adapter advances the archived raw-frame-derived books only
through the current counterfactual event time, using decode availability. A
future disappearance/replenishment cannot restore past liquidity. The adapter
does not treat this conservative L2 model as observed matching-engine behavior.

## Native study runner integration

The runner feeds only resource-selected candidates into shared worlds and writes
`shared_scenarios.jsonl`, per-world receipts, event-trace hashes, censoring reasons
and pending-work counts into its immutable study output. It keeps independent
scenario evidence separately for attribution.

Candidate views are pinned before future execution, and immutable candidate data
is shared across worlds rather than duplicated per arm. Explicit world, pending
job, pending serialized-byte, episode and depth-debit limits fail closed. These
are logical storage bounds, not a measured process-RSS or London latency claim.

Capital remains encumbered under the existing no-unverified-release policy.
There is still no realized portfolio-PnL total, settlement guarantee or capacity
curve. The report's economic decision remains null.

## Adversarial evidence

- Two independent simulations can each consume the same displayed quantity;
  the shared venue allows it only once.
- Interleaved sequential orders execute in arrival order, not cycle order.
- Missing later-leg data preserves the known earlier fill and unknown exposure.
- A full fill finishes at its last entry arrival without inventing an unwind.
- Equal-time ordering and replay are deterministic under the declared policy.
- An end-to-end fixture creates two separately funded entries that both need the
  same unchanged bid to unwind. Independent scenarios both report closed unwinds;
  the shared world correctly reports one closed unwind and **five shares still
  exposed** for the second episode, with unknown realized PnL. The fixture also
  passes through the actual C++ raw-WS decoder before the runner executes it.

These are synthetic correctness fixtures, not London fills or profitability.

## Verification

- 96 scoped execution/graph/runner Python tests passed after the kernel changes.
- The native decoder → runner/CLI → competing-unwind integration passed in
  Release, Debug, combined ASan/UBSan and ThreadSanitizer. Native implementation
  code was not changed in this checkpoint.
- Final full Python suite: **2,494 passed, 1 existing skip, 384 existing numerical
  warnings**, using `/usr/bin/python3 -m pytest -q --disable-warnings`.
- Configured Release CTest: **408/408 passed**. Unchanged native targets used
  cached binaries; this is not a clean Linux release/security attestation.
- `git diff --check` passes. Frozen champion lane, economics and multi-engine
  files have no diff. Tests and execution authority were not weakened.

## Remaining high-value work

1. Causal ACK/cancel and settlement/release evidence; actual current venue fee,
   delay and matching semantics; control-plane metadata/fee invalidation events
   propagated into arrival eligibility. Scenario timestamps are not venue ACKs.
2. Inventory/cash lifecycle and capital-time attribution, per-budget quantity
   re-optimization, global sizing proof and full champion parity before reporting
   executable capital/PnL capacity.
3. Independent semantic breadth, maker queue/legging calibration, hourly evidence
   and the four automatic SOTA documents, plus actual champion load benchmarks.
4. Clean release/security gates, official exact-SHA London deployment and the
   bounded causal economic study. No new London health receipt was obtained here;
   the preceding read-only SSH check failed authentication.

`research_decision = null`; the full objective remains active and incomplete.
