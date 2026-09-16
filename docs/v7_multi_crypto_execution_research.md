# Multi-crypto execution and research: complementary workstream

Base: `940be03872027e3219a2bf1db5e59263d66110a9`.
Branch: `research/v7-multi-crypto-execution-research-20260917`.

The parallel `research/v7-multi-crypto-lead-lag-20260916` worktree owns venue
adapters, registry/discovery, PM book selection and London preparation.
This branch owns offline FAK validation, latency/capacity replay, causal
features/model validation, accounting audits and a reservation projection
for the existing global coordinator. It does not modify the other worktree,
production process manifest, launcher, frozen BTC configuration or live data.

There is no additional executor, ledger writer, allocator or portfolio owner.
The native counterparts already exist: `simulate_taker_paper` and
`SleeveCapitalAccount`. The Decimal reference is for offline auditing and
counterfactual research, not a replacement service.

The new reservation path is opt-in and not connected to the frozen forward.
No newly supported ticker receives execution authority automatically.

All new execution results remain simulated. Unknown arrival state and missing
settlement are not zero PnL. Source availability precedes every admitted
feature. A repricing prediction is not a settlement probability.

Status and exact commands are updated after the test/integration gates.

## First implementation checkpoint

Implemented: Decimal arrival reference; deterministic event-driven latency and
capacity report; prior-only normalized shock; immutable residual ridge model;
chronological purge; shared-market/shock/time clustering; partial FAK, no-chase,
fee-incidence separation, explicit unresolved resolution and missing-data gates.

Executed on the isolated development host:

```
python3 tests/test_v7_lead_lag_replay.py          # 56 tests
python3 tests/test_v7_lead_lag_research.py        # 22 tests
python3 tests/test_v7_lead_lag_replay_report.py   # 23 tests
```

All 101 passed. These are synthetic correctness tests, not economic evidence.
Full repository/native CI is a separate gate. Nothing is merged or deployed.
Durable coordinator integration and ledger-history audit remain in progress.

Explicit conventions: per-price-level fee rounding, no replenishment credit for
consumed depth, declared positive base network latency, modeled redemption after
resolution availability. No convention is presented as observed exchange truth.

Official semantics consulted:
https://docs.polymarket.com/trading/fees
https://docs.polymarket.com/trading/place-orders
https://docs.polymarket.com/concepts/resolution

## Durable coordinator checkpoint

The existing coordinator now exposes `coordinate_reserved_paper`. It is opt-in:
no launcher, process manifest, frozen BTC route, or active authority is changed.
Its existing `coordinate()` policy remains the gate. New ticker support in an
offline module does NOT expand that gate.

`ReservationProjection` appends only through a supplied synchronous canonical
writer callback. It is not a writer or a service. A durable submit fence blocks
resubmission after restart. Partial FAK completion requires the actual canonical
fill records and terminal order record, including coverage of every partial.
FINAL requires a bound authoritative-resolution proof and exact cash identity.
Unknown submission never expires into a zero-cost recovery. Duplicate attempts
and traded markets remain remembered across code SHAs.

The owner must provide a complete account checkpoint, including other lanes'
OPEN exposures and RESERVED cash. A subsequent foreign monetary event marks
that checkpoint stale and blocks new reservations/submission until reconciled.
This is deliberately fail-closed; automated whole-account checkpoint migration
is NOT implemented or claimed. Risk-reducing settlement remains possible.

New test command:

```
python3 tests/test_v7_coordinator_reservations.py  # 33 tests
```

All 134 new synthetic tests passed at this checkpoint. Existing coordinator,
opportunity and repository-shape regression scripts also passed. Tests include
the actual existing canonical writer and replay after a different code SHA.

PR: #960 (DRAFT). Runtime deployment: NOT DEPLOYED.
The historical-ledger audit is NOT implemented; the attempted audit-file write
was rejected by the remote command filter. No permission configuration changed.
No whole-portfolio reconciliation, observed market replay, economic edge,
Linux/native CI success or full program completion is inferred from unit tests.
