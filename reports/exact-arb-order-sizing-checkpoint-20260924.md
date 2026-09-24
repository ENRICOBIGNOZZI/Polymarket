# Exact order-lattice sizing checkpoint

Branch: `research/unified-exact-arb-graph`.
Base SHA: `f50147e18e02ebed76cabe900b708e4d9479e603` plus uncommitted changes.
PAPER / SHADOW only. No commit, merge, deployment, promotion or execution authority change.

## Reproduced defects

The previous runtime wrapper rounded down the legacy marginal sweep's result
and ran that sweep again. Its quantity was explicitly not a proven optimum.
Three deterministic regressions now distinguish the new model from that path:

1. Two one-level asks at .5000 and .4991, .0005 rate/exponent zero, .0005
   reserve, .10 shares depth and .01 total-order quantum. The legacy sweep
   rejects the whole-depth basket. A .01-share order has model net PnL .000004
   because its raw fee is below the model's minimum fee cutoff. The bounded
   optimizer finds it. This synthetic tiny order is a mathematical regression,
   NOT a claim about an actual market's minimum order or business viability.
2. A coefficient-two leg with a one-microshare cheap level and 19,999 microshares
   at the next price must retain that first microshare. Fill fragments do not
   need to satisfy the total-order lot quantum.
3. Splitting one leg's .01-share price level at the other leg's .005-share
   breakpoint waived a fee in the legacy basket-slice accounting. The new
   per-L2-level model charges .00001 for that leg and matches the generic
   execution model's fragmentation convention. Actual venue fragmentation
   remains unverified; L2 levels do not identify individual maker orders.

## Native implementation

`include/pm/v7_exact_arb_order_sizing.hpp` now evaluates cumulative order costs
using integer microshares, E4 prices, rational coefficients and signed 128-bit
pico-PUSD arithmetic. It rounds displayed PnL down and capital up to micro-PUSD.
Each L2 level contributes one rounded fee; another leg's level transitions do
not fragment that fee. Fee arithmetic overflow, unsupported terms, invalid
books, stale/skewed books, incomplete depth, invalid quantities, unavailable
inventory and unverified transformations still fail closed.

Feasible quantity bounds include every leg's complete available depth, minimum
order, order quantum, supplied inventory, quantity limit, transformation capacity
and supplied capital. Capital is monotone in quantity under this model, so a
bounded integer binary search identifies its upper feasible quantity.

The search uses a mathematical upper bound, not a first-negative-fee shortcut:

- Let `f(q) = raw_pnl(q) - reserve(q)`. Sorted asks/bids and positive coefficients
  make this function concave, including on the admissible order lattice.
- Let `h(q)` be cumulative per-L2 rounded fees. It is nonnegative and monotone.
- On a lattice interval `[a,b]`, `max(f(q)) - h(a)` bounds every net PnL above.
- The leftmost raw maximum is found on that lattice. Depth-first bisection
  prunes only using this valid bound and preserves the smallest-q tie rule.
- The zero-order/no-trade alternative has net PnL zero.

Two fast certificates reduce unnecessary search. If `f(q_min) <= 0`, concavity
and `f(0)=0` prove no larger order can become profitable after nonnegative fees.
For wide edges, the last raw-minus-reserve increment lower-bounds all earlier
increments. Fee rounding error lies in `(-1,+1/2]` fee units; bounding incremental
fees by their maximum unrounded increment plus 1.5 fee units per potentially
touched level can prove net PnL strictly increasing. This selects the maximum
feasible order without a full branch search. Neither certificate lowers reserve
or discards fees.

The default work budget is 512 quantity evaluations; callers cannot exceed 4,096.
The DFS stack has 64 fixed entries. Each quantity evaluation is bounded by the
sum of leg depths, not graph cardinality. There are no heap allocations, file
operations or network calls in this sizing path. The existing feed allocation
contract still passes. The old marginal sweep is retained as a diagnostic
baseline, including its quantity in evidence; its monetary result is not used
to score the new optimizer's incumbent.

If the budget runs out, a feasible positive incumbent may be emitted, but
`global_size_optimum_proven=false`. With no positive incumbent the rejection is
`SIZING_INCOMPLETE`, not evidence of no opportunity. The upper bound is published
only when actually computed and representable; otherwise it is null.

## Evidence and scope

Native observations and full evidence record:

- `sizing_model = PER_L2_LEVEL_5DP_EXACT_ORDER_LATTICE_V1`;
- `sizing_proof_scope = RECORDED_MODEL_ONLY_NOT_VERIFIED_VENUE_EXECUTION`;
- exact-model optimum flag, work count, exhaustion flag and nullable net bound;
- legacy-sweep quantity separately from the selected order quantity.

The Python bridge retains this scope. The evidence reducer reports model proofs
and exhausted searches separately. An exhausted search without an accepted
incumbent censors the pre-allocation episode instead of ending it as an observed
negative; subsequent positives are left-censored, not invented new arrivals.
Top-of-book diagnostics keep their separate descriptive scope. Hourly/live
coverage or filled-PnL claims are not introduced.

`economic_execution_verified` and `venue_execution_verified` remain false.
Optimizing this recorded model does not verify fee collection/ties/fragmentation,
order matching, simultaneous fills, transformations or capital release.

## Independent oracle and champion comparison

`tests/test_v7_exact_arb_order_sizing.cpp` uses arbitrary-precision rational
arithmetic with an independent fee implementation and exhaustive quantity
enumeration. Seeded fixtures cover 2–16 legs, rational coefficients, BUY/SELL,
multiple levels, exponents 0–2, reserves, minima, capital and inventory limits.

- 3,000 rational-oracle trials: 1,475 admitted/proven domains, zero exhausted
  searches; remaining fixtures have no admissible positive opportunity. Every
  admitted optimum and conservative reported money agree with the oracle.
- 3,000 aligned-depth zero-fee binary comparisons against the actual frozen
  champion: zero quantity differences; displayed PnL differs by at most one
  micro-PUSD because the new output floors exact values conservatively.
- 3,000 nonzero-fee binary comparisons: **122 quantity differences** and **827
  displayed-money/model differences at the champion's selected quantity**.
  These count differences, not venue profits. The new selection never has
  lower exact-model PnL than the champion quantity scored under that same model.
  Full champion economic parity is therefore **not established**. Fragmentation,
  floating rounding and conservative output rounding must not be conflated.

The three frozen champion source files remain byte-for-byte unchanged relative
to HEAD. Existing tests were not removed or weakened; assertions that no sizing
proof can ever exist were replaced by scoped model-proof and zero-authority
invariants. The new native target is part of the review CI matrix.

## Actual runtime sizing benchmark

The benchmark now measures the actual order-constrained runtime function,
separately from the legacy graph sweep. The legacy ratio is explicitly labelled
as such and is not substituted for the new function's ratio. The final local
Apple M4 / arm64 Release run uses 100,000 samples of four-level synthetic
wide-edge books; complete distributions, source hashes and binary hash are in
`exact-arb-order-sizing-benchmark-20260924.json`.

| Function | p50 ns | p99 ns | p99.9 ns | Quantity evaluations |
| --- | ---: | ---: | ---: | ---: |
| Frozen champion binary sweep | 250 | 792 | 875 | N/A |
| Exact order-lattice binary | 542 | 625 | 708 | 3 |
| Exact order-lattice 3-leg | 750 | 792 | 958 | 3 |
| Exact order-lattice 4-leg | 959 | 1,041 | 1,209 | 3 |
| Exact order-lattice 8-leg | 17,209 | 17,917 | 27,666 | 59 |
| Exact order-lattice 16-leg | 33,625 | 38,417 | 45,334 | 59 |
| Binary raw-nonpositive certificate | 125 | 167 | 208 | 1 |

All sampled positive shapes certify their model optimum. The 8/16-leg shapes
do not satisfy the loose monotonic-fee bound and still search. These measurements
do not certify worst-case deep-book latency, sustained throughput or champion
side-by-side non-regression. The binary p50 is slower than the champion even
though this run's p99 is lower; one sequential local benchmark is not evidence
of a speed advantage. Wider-basket search cost remains a material open gate.

## Validation and remaining gates

All modified native consumers were rebuilt in Release, Debug, combined ASan/UBSan
and TSan. Six scoped native tests (including both C++/Python integrations) pass
in each non-Release variant. Release's configured full CTest suite passes
**413/413**. The full local Python suite passes **2,652 tests**, with one existing
skip and 384 existing warnings. Existing observer compile/link warnings remain.
These are local gates, not clean official Linux/security/deployment CI.

No London evidence was obtained. The fresh read-only SSH service probe still
fails authentication (`Permission denied (publickey,password,keyboard-interactive)`).
No runtime health, current deployment SHA or champion live latency is inferred.

Remaining: authoritative fee/execution semantics and complete champion parity;
worst-case deep-book/affected-relation sizing load and live side-by-side latency;
capital lifecycle/capacity, broader independent semantic attestations, maker
calibration, archive retention, hourly multi-session evidence, official release
gates and the bounded London PAPER economic study. A model optimum is not a
profitability result. `research_decision = null`; the objective remains active.
