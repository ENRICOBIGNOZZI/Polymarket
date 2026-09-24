# Venue timing and public terms checkpoint

Branch: `research/unified-exact-arb-graph`.
Base SHA: `f50147e18e02ebed76cabe900b708e4d9479e603` plus uncommitted changes.
PAPER / shadow only. No orders, promotion, deployment or champion economics changes.

## Correctness gap and authoritative rules

The generic N-leg simulator previously modeled transport and inter-leg skew but
not token-specific venue holds. A public-book fill at wire arrival can precede
the venue's matching time, manufacturing an apparent capture. Different holds
can also reorder matching across legs and invalidate index-ordered simulation.

The official [order lifecycle](https://docs.polymarket.com/concepts/order-lifecycle)
documents a 250 ms taker hold for selected markets and separate configured sports
delays. The pending order cannot be cancelled during a delay; matching requires
revalidation afterwards. Matching and final on-chain settlement are distinct.

The official [CLOB OpenAPI](https://docs.polymarket.com/api-spec/clob-openapi.yaml)
explicitly defines omission of compact `itode` as false. That narrow schema rule
does not imply that omitted `seconds_delay`, fees or other fields mean zero.
Configured sports-delay activation and combinations are not inferred here.

The [order documentation](https://docs.polymarket.com/trading/place-orders)
defines the public book's `min_order_size` in shares. The new collector obtains
that field directly and requires agreement with both CLOB market descriptors;
it does not substitute Gamma's quantity field or a USD market-buy amount.
The [fee documentation](https://docs.polymarket.com/trading/fees) alone does not
resolve exact tie handling, matching fragmentation or cash/share collection
ambiguity. Those remain explicitly unverified.

## Implemented execution changes

`v7_exact_arb_execution_timing.py` provides one exact-rational schedule shared by
the independent simulator, shared-liquidity event queue and runner horizon.

- Submission, wire arrival, matching and modeled result reception are separate.
- Optional `venue_delay_ms_by_token` must cover every required basket token.
  Missing tokens censor the scenario; they do not acquire a zero hold.
- An absent entire profile is labeled `ZERO_HOLD_UPPER_BOUND_UNVERIFIED`.
- Optional `ack_delay_ms` models response travel AFTER matching. Sequential
  submission waits through matching, this response delay and configured skew.
- Parallel and batch matching is sorted by actual scheduled matching time,
  with deterministic leg-index ties, not by token order in the relation.
- Each unwind incurs its own transport and token hold. All unwind limits are
  pinned when entry results are known, before consulting later matching books.
- Entry/unwind identity is matched by original leg index, preventing a reordered
  fill from being unwound using another token's terms.
- Event ordering, partial-fill preservation and shared depth debits remain in
  the same kernel. Response waits are separate events rather than look-ahead.
- Original wire `arrival_timestamp_ms` and new `match_timestamp_ms` are explicit;
  book age is evaluated at matching, not before the hold.

The model is now `CAUSAL_LIMITED_ORDER_V4_VENUE_TIMING`. Normalized hold/response
assumptions are part of arm/cycle/study identities and automatic reports. The
runner source digest includes the shared timing module. Legacy scenario rows
cannot be pooled into the new model. Unknown matching terms remain
`NON_EXECUTABLE_UNVERIFIED_VENUE_TERMS`; no scenario is promoted to a venue fill.

## Public read-only source evidence

`v7_exact_arb_venue_terms.py` fetches only allowlisted public GET endpoints:
compact CLOB descriptor, full CLOB descriptor and each token's public book.
Requests are unauthenticated, bounded to 1 MiB per response and five-second
timeouts, with no redirects. Duplicate JSON keys, non-finite values, token or
condition collisions, closed markets, inconsistent minima/ticks, missing fee
terms and invalid clocks fail closed. Source failure never retains a prior
successful projection.

Receipts preserve request timing, original response bytes as text and their
SHA256 hashes. Publication is immutable. An integrity hash is NOT independent
source attestation. Separate requests are non-atomic, have no invented forward
validity lease and are not mapped implicitly into a native session clock.

A fresh public check produced:

- Condition: `0xa467b14d51f01b957109d9cbb1d6c124fab2a089d52ed8f471d23c2812e743b7`.
- Fee rate `1/25`, exponent `1`; minimum `5` shares; tick `1/1000`.
- Explicit sports delay `0`; compact `itode` omitted under the documented rule.
- Receipt [13ea31fc…](evidence/exact-arb-venue-terms-20260924/13ea31fcd7d83afac377a92198306a0e18d73dbbae1285a726fa43491ed3637d.json).
- An earlier default-client HTTP 403 is also archived as an UNVERIFIED failure,
  [4d768ded…](evidence/exact-arb-venue-terms-20260924/4d768ded99893f951a65e11aeadbb9bfcca7ffb015b51b2495bd78b101be2375.json).

This is one market's metadata observation, not causal exchange coverage, live
opportunity evidence, fee granularity verification or an execution guarantee.

## Verification

Final validation:

- **2,624 Python tests passed**, 1 existing skip, 384 existing warnings.
- **411/411 configured Release CTest tests passed** after CMake reconfiguration.
- Both native/Python bundle and arrival-runner integrations passed again in
  Debug, combined ASan/UBSan and TSan after the last analysis-code change.
- `git diff --check` passes. Frozen champion lane, economics and multi-engine
  files have no diff.

No C++ source changed in this increment; native binaries were cached. These are
local configured tests, not a clean Linux release/security gate, measured London
latency, full champion parity or economic validation.

Targeted tests include heterogeneous holds, sequential responses, delayed unwind,
missing profiles, shared-event ordering, native study horizons and 100 seeded
2–16-leg schedules. Source adversarial tests cover omission rules, malformed
responses, fee absence, sports ambiguity, identity conflicts, clock failures,
failed refresh and immutable-artifact corruption.

## Remaining gates / next action

The collector is not yet a continuous, session-bound control-plane producer.
Its standalone receipt is intentionally NOT automatically attached to an old
candidate or accepted as validity at a future matching instant. Connect these
terms to graph admission, token/condition identity and recorded invalidation
history before enabling an economically eligible delay profile. Sports active
state and mixed holds need authoritative evidence; otherwise remain censored.

Fee fragmentation/collection, exchange ACK/rejections, inventory availability
after fills and settlement release remain unresolved. Global sizing/parity,
independent relation breadth, maker calibration, load benchmarks, hourly
multi-session evidence, official clean release gates and the bounded London
economic study remain open.

A fresh read-only SSH service probe again failed authentication for `polymarket`.
No current deployment/service health was inferred. No merge or deployment was
attempted. `research_decision = null`; the full objective remains unfinished.
