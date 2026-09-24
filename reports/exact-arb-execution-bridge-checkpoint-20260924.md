# N-leg order realism and native candidate bridge checkpoint

Branch: `research/unified-exact-arb-graph`.
Base SHA: `f50147e18e02ebed76cabe900b708e4d9479e603` plus uncommitted changes.
Engineering only. No merge, deployment, authenticated execution or real orders.

## Defects found and corrected

1. **Unbounded arrival-price sweep.** The generic N-leg simulator previously
   walked arrival liquidity without an order limit chosen at decision time.
   It now pins the worst required decision-book level per leg and never fills
   beyond it. The Python reference observer persists the exact decision books
   and their digest. Missing/altered decision snapshots fail closed, rather than
   reselecting potentially later books with the same millisecond timestamp.
2. **Mutable causal history.** `CausalBooks` now copies snapshots at both ingest
   and lookup. A caller cannot rewrite old prices by mutating a reused dictionary.
3. **Implicit partial-fill policy.** Scenarios explicitly distinguish FAK and
   single-order FOK. A failed FOK consumes no displayed liquidity. Neither mode
   makes the basket atomic; batch scenarios above the configured 15-order bound
   are censored. Sixteen-leg parallel research remains a distinct scenario.
4. **Optimistic unwind.** The unwind limit is frozen at its own submission
   decision, not chosen from the future arrival book. Only venue-quantized order
   quantity can be unwound; below-minimum and fractional residuals remain exposed.
   The report preserves token exposure and exact statewise residual payoffs.
   Missing later evidence preserves known entry fills, with residual risk unknown.
5. **Unsafe economic defaults.** The simulator rechecks the finite-state proof,
   rejects reserve below the existing five-basis-point guarantee-scaled baseline,
   and requires explicit venue rounding for nonzero fees. Unknown tick/minimum
   terms cannot enter the scenario. Zero filled legs incur no invented unwind
   or reserve cash expense. Frozen champion economics are unchanged.
6. **Alternative-scenario PnL double counting.** Mode-level PnL aggregation is now
   null. Latency/skew/order-policy arms remain separate, with distinct conditional
   locked PnL and closed-counterfactual PnL sample counts. Unknown totals remain
   null. Previous execution-model rows are excluded from current summaries, with
   an explicit historical-row counter. Scenario IDs bind economic inputs and
   all modeled timing/order parameters.
7. **Time truncation.** The generic model preserves exact rational millisecond
   timestamps, including native nanoseconds. A regression places unfavorable and
   favorable updates on opposite sides of arrival within the same millisecond;
   only the causal one contributes. This does not improve precision of old tapes.
8. **Crossed books.** Both the generic sweep and native candidate bridge reject
   crossed/locked books instead of treating inconsistent bid/ask state as cheap
   executable liquidity.

## Native-to-simulator boundary

`scripts/v7_exact_arb_native_execution_bridge.py` provides an off-path candidate
constructor shared with the existing N-leg model, not a binary adapter.

- Joins an observed pre-allocation episode start to its exact full-evidence row
  by model/session/sequence/bundle/time and canonical economic identity.
- Initial/left-censored positive segments are not invented arrivals. Future
  episode closure, lifetime and right-censoring are never selection features.
- Uses the existing content-addressed bundle reader; rechecks statewise payoffs,
  token membership, leg versions, ticks, actual depth, quantity precision,
  minimum orders and source/relation/freshness deadlines.
- Native full-book rows now label each book with its token ID, and observations
  carry the separate relation deadline. Mapping a decoder handle to a token is
  performed by the control-thread writer, not the feed thread.
- Computes order submission from **decision end**, not receive or decision start.
  It rejects a candidate whose computation finishes after the pinned validity
  window. Monotonic nanoseconds remain exact rational milliseconds downstream.
- Preserves `resource_admission_verified=false`, `venue_execution_verified=false`
  and the native global-sizing-proof flag. It does not reserve inventory or funds.
  SELL receives no synthetic inventory grant.

Crucially, the bridge **does not construct future books from positive-only native
full-evidence rows**. Such a tape omits intervening negative states and is not a
valid arrival history. Tests supplying future histories explicitly use synthetic
continuous histories; they are not London fills or profitability evidence.

## Validation

- New tests cover pinned limits, FOK/FAK, depleted same-version liquidity, partial
  states, dust/minimum orders, SELL inventory, false proofs, reserve floors,
  unknown fee precision, immutable history, same-millisecond look-ahead, separate
  scenario arms, exact native timing, source expiry and episode/full-book joins.
- The final bridge/order/evidence/adversarial/graph scoped run passed 104 tests.
- Final full Python suite: **2,428 passed, 1 existing skip, 384 existing numerical
  warnings**, using `/usr/bin/python3 -m pytest -q`.
- Final configured Release CTest: **402/402 passed**. Unchanged native targets are
  cached; this is not a clean Linux release build or official deployment
  attestation.
- Runtime, loader/WS writer and Python bundle roundtrip passed 3/3 each in Release,
  Debug, combined ASan/UBSan and ThreadSanitizer after the native field changes.
- The new test for persisted decision books initially lacked a `json` import;
  the import was corrected and all 84 scoped tests rerun. No assertion was relaxed.

## Live access and remaining gates

The current read-only `ssh polymarket` service query again returned
`Permission denied (publickey,password,keyboard-interactive)` for
`enrico@100.104.183.109`. It established no London service, SHA, clock, fencing,
worker, tape or performance fact. The user was asked for restored access or the
official already-authorized SSM/deployment channel, without requesting secrets.

Next priorities remain:

1. Native continuous arrival replay with feed-side loss accounting, frame
   completeness and monotonic-time lineage; do not join tapes on rounded wall
   time or mistake positive-only snapshots for continuous book evidence.
   Inspection found that the legacy observer's `observer_sequence` is assigned
   by the writer, after producer-queue admission: it cannot alone attest that
   the producer lost no events. The native replay contract must bind a feed-side
   sequence and frame completeness rather than reusing that counter as proof.
2. Current verified venue terms and mandatory taker delays, fee-fragmentation
   bounds, ACK/cancel timing, resource reservations and capital-time costs.
3. Wire the episode bridge into bounded hourly evidence reduction, then benchmark
   observer/writer load against the actual champion before removing Python graph
   traversal. Complete global sizing/parity and independent semantic breadth.
4. Finish the four auto-updated research documents, complete release/security
   gates, officially deploy an exact SHA and collect the bounded London sample.

The N-leg output still says `TRANSPORT_SCENARIO_NOT_VERIFIED_VENUE_EXECUTION`.
Mathematical identity and synthetic fill scenarios are not verified economic edge.
`research_decision = null`; the complete goal remains active.
