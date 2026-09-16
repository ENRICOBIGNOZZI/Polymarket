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
