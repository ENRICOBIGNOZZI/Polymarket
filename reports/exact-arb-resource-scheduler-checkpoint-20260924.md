# Causal resource planning checkpoint

Branch: `research/unified-exact-arb-graph`.
Base SHA: `f50147e18e02ebed76cabe900b708e4d9479e603` plus uncommitted changes.
PAPER-only, zero-authority research. Frozen champion economics are unchanged.
No authenticated order, actual capital, promotion, merge or deployment.

## Implemented

`scripts/v7_exact_arb_resource_scheduler.py` supplies a bounded exact-rational
resource selector, a native-candidate funding adapter and a durable shadow-hold
journal. The offline native study runner now uses it and includes
`resource_plans.jsonl` in its immutable, hash-verified output.

### Selection

- Each request supplies a vector of required resources, not just one PUSD scalar.
  The optimizer supports inventory, complete sets, claims, conversion/merge slots,
  pending-capital pools and per-price displayed-depth claims as distinct keys.
  Missing capacity is unknown/ineligible, never manufactured or zero-filled.
- Branch-and-bound chooses the feasible subset maximizing the declared causal
  conditional score. It is exact when its certificate closes. Explicit limits
  bound batch size, search nodes and constraint entries examined. Exhaustion
  returns a feasible incumbent, an upper bound and `optimal_value_proven=false`
  where a better objective remains possible; it never silently claims optimality.
- Duplicate economic portfolios are counted once inside a decision batch.
  Conflicting duplicate requirements or scores fail closed. Input-order changes
  do not change the selection. No future fill, unwind or episode lifetime enters
  the score or admission decision.

### Native funding/depth adapter

- Rechecks the statewise proof, pinned decision books, order quantity/minimum,
  tick, fee terms and reserve floor. Reserves funding against pinned worst-leg
  limits rather than assuming later favorable execution prices.
- Full-depth demand is represented per consumed token/side/price. The test suite
  exercises 1,000 distinct depth resource claims, not a top-of-book proxy.
- Depth resource identity deliberately excludes book `state_version`: a heartbeat
  or unrelated book update cannot create another copy of the same displayed size.
- Funds entry fees and a conservative unwind-fee cushion. SELL also requires
  prefunded claim inventory and independent unwind-buy funding; it never assumes
  shorting or immediate reuse of sale proceeds.
- Under the repository's declared `VENUE_5DP` rounding model, arbitrary fee
  fragmentation is bounded by **4/3 of aggregate raw fees**. For raw fragment x
  at least one increment h, half-up rounding has maximum ratio 4/3 at x=3h/2;
  smaller-than-h fragments charge zero. Summing this inequality requires no
  guessed fragment count. Tests cover the tight boundary and 1,000 seeded random
  partitions. This is a mathematical model bound, NOT current venue attestation.
- Funding for an unknown unwind is reserved, not booked as if every completed
  basket necessarily incurs unwind cost. The selection score remains explicitly
  `WORST_LIMIT_FULL_FILL_NET_PNL_NOT_EXPECTED_VALUE`.

### Durable shadow holds

- Reuses the existing `ResourceLedger` quantity admission logic; it does not
  modify the champion allocator or canonical trading ledger.
- SQLite transactions serialize writers and atomically persist batch decisions
  and all their holds. Restarts and exact replay do not reserve a second time;
  conflicting replay fails. Model mismatches and clock reversal fail closed.
- Snapshot leases expire admission, not existing holds. Financial capacities
  shrinking below encumbered amounts do not free capital or permit other new
  allocation. Failure midway through a batch rolls back all its writes.
- Release time is unknown and reservations persist indefinitely until a separate
  causal settlement/release adapter exists. No TTL, market end or transformation
  duration is misrepresented as confirmed cash availability.

## Runner integration and interpretation

The native session runner produces separate decision-time planning sensitivities
for hypothetical PAPER budgets of $1k, $10k and $100k by default, configurable
through `--capital-budgets`. Only candidates available at the decision timestamp
compete; the planner does not read their subsequent execution-scenario results.

These plans are **not capital/PnL capacity curves**. Quantities are the frozen
candidate quantities, resources have no verified release, and execution legs are
not yet interleaved across competing episodes in one shared counterfactual venue.
The independent latency-arm scenarios therefore remain separate from resource
plans; portfolio PnL, expected PnL and capital efficiency stay null. Increasing
capital does not fabricate extra depth or linearly scale profits.

## Validation

- 40 scoped scheduler/runner tests passed, including a 250-case seeded comparison
  against exhaustive rational subset enumeration, tight fee-rounding bounds,
  concurrent SQLite writers, failure rollback, source shrink, restart/expiry,
  duplicate paths, unknown capacity, bounded-search certificates and full depth.
- The existing native-decoder → runner → CLI integration exercises the additional
  resource-plan artifact and identical repeat publication.
- Final full Python suite: **2,482 passed, 1 existing skip, 384 existing numerical
  warnings**, using `/usr/bin/python3 -m pytest -q --disable-warnings`.
- Configured Release CTest: **407/407 passed**. Unchanged native targets used
  cached binaries; this is not a clean Linux release/security attestation.
- Native-decoder/runner/CLI integration passed in Release, Debug, ASan/UBSan and
  ThreadSanitizer. The native implementation was unchanged in this checkpoint.
- `git diff --check` passes. Frozen champion lane, economics and multi-engine
  files have no diff. No tests or safety limits were relaxed.

## Remaining economic/engineering gates

1. A single causal multi-episode execution event queue with shared liquidity
   depletion, ACK/cancel evidence and release/settlement transitions. Current
   irreversible holds are a conservative planning diagnostic, not that engine.
2. Independently verified current venue fees/timing and transformation readiness;
   measured capital lock, inventory valuation and unconditional legging-risk EV.
3. Per-budget quantity re-optimization, global sizing proof/champion parity and
   actual depth-limited PnL capacity curves after jointly feasible execution.
4. Independent relation attestations, four automatic research documents, complete
   hourly reporting, maker calibration, champion load benchmarks and release gates.
5. Official exact-SHA London deployment and the bounded causal economic study.

A fresh read-only London service check still returned SSH authentication failure
for `enrico@100.104.183.109`. No runtime-health or economic conclusion follows
from that failure; repository work continues independently.

`research_decision = null`; the full research objective remains incomplete.
