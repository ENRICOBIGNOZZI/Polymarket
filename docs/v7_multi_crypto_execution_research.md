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

## Read-only live accounting checkpoint

A byte-for-byte snapshot of the active canonical ledger was copied read-only;
the active ledger itself was not edited. Snapshot SHA-256:
`f7aeeb52d7f5c94b1b361462bd2f069db5a207393244dfa48324c86265f1ac69`.

Observed lead-lag state in that snapshot: 14 PAPER fills/positions, 13 FINALs
and one still-open position. The runtime status independently reported the same
14 entries, 13 settled, one open, 10 wins and reported realized PnL 17.346885.
Every historical lead-lag fill uses `recorded_ts_ms = decision_ts_ms + 1`; this
is synthetic event ordering and is explicitly excluded from latency evidence.

A new read-only audit checked the 13 terminal markets against fresh official
Gamma resolution records. All 13 reconcile to their recorded payout/PnL within
the legacy float tolerance. Supported contribution over those 13 resolved
positions is 17.346884999999999926; the remaining position is `UNRESOLVED`, not
zero PnL. No zero-recovery/forced-flat/conservative-terminal marker was found
in the lead-lag records examined. Current FINAL rows predate embedded raw
resolution-proof hashes, so the audit keeps external response hashes rather
than rewriting history.

This is deliberately a lead-lag long/hold audit, NOT a complete account
checkpoint. `whole_portfolio_reconciled=false`; therefore it cannot itself
unlock the durable reservation projection. Corrections, when required by future
audits, are only proposed append-only records and are never applied in place.

## Global monitoring reconciliation fix

The active exporter was audited read-only after the lead-lag cohort settled.
Canonical ledger/economics and portfolio totals agreed, but monitoring reported
`strategy_realized_pnl_divergence:CRYPTO_SETTLEMENT_ENGINE`. Root cause: the
state-side comparison used only the external-fair component (3.4) and omitted
the lead-lag component (17.58026), while canonical economics correctly grouped
both under `CRYPTO_SETTLEMENT_ENGINE` (20.98026).

The isolated branch now aggregates state PnL by component under the same engine
and exposes the components explicitly. If canonical economics says the
`lead_lag_taker_v1` family is present, missing/unsafe lead-lag state yields
`strategy_realized_pnl_unverifiable:CRYPTO_SETTLEMENT_ENGINE`; it is never
silently interpreted as zero. No production process or frozen BTC file is
changed by this monitoring correction.

## Bounded direct IPC checkpoint

A new opt-in Unix-stream bridge removes candidate inbox files and receipt-file
polling from the new fast-forward path. The accept thread performs framing only;
the existing coordinator thread drains the bounded queue and remains the sole
economic decision owner. Queue-full, malformed frames and handler faults return
explicit fail-closed replies. The socket is mode 0600 and an existing socket
path is never stolen or silently unlinked.

`process_fast_forward_ipc_reserved` binds the direct reply to the already-built
durable reservation projection. A PAPER TAKE is returned only after the
supplied canonical append callback has durably recorded the reservation. The
path writes no receipt file. A call-graph test patches `open()` to fail and
proves Unix IPC -> same coordinator -> durable reservation -> direct response
without hot-path file access.

This transport is implemented but NOT activated in the current process manifest
or frozen BTC launcher. Full-account checkpoint construction remains the gate
before a new PAPER lane may use it with entry authority. The current Python
framing is an integration bridge, not a claim that the final C++ typed-wire
latency target has been reached.

## Shared risk guard correction

The same component-aggregation defect affected the account risk guard: its
`CRYPTO_SETTLEMENT_ENGINE` equity read only the external-fair router and ignored
lead-lag positions/PnL. The isolated branch now reconciles the lead-lag status
and durable state before adding it to the shared engine equity.

Settled lead-lag PnL is added once. An OPEN lead-lag position is marked at zero
recovery value for RISK only, so its entry cost plus entry fee is treated as the
full amount at risk until official settlement. That conservative mark is not a
FINAL and is never reused as settlement evidence. Status/state count, protocol,
model identity and terminal-PnL sums must reconcile; otherwise the crypto engine
is fatal-to-portfolio and new risk fails closed.

A read-only copy of the then-current live state (one open lead-lag position)
produced account equity 14019.872195, with lead-lag realized PnL 17.58026 and
1.108065 of open cost-at-risk conservatively marked to zero. This validation ran
on the isolated copy only; production risk state was not rewritten.

## Multi-crypto residual benchmark integration

The six-asset labeled-row contract from the parallel feature/label workstream is
now consumed directly by `v7_multi_crypto_residual_benchmark.py`. Every row hash,
model/policy/feature identity and feature-availability timestamp is validated
before training. The target is `future PM yes mid - current PM yes mid`; it is
not treated as settlement probability.

Ablations are nested and share one frozen common time split across all assets:
`PM_ONLY -> OWN_EXTERNAL -> ORACLE -> LEADERS -> DERIVATIVES`. Ridge is supplied
as a fixed preregistered value; the benchmark never selects a winner from the
test partition. Entirely missing features may be dropped using TRAINING data
only, and the drop list is reported. Native open-interest is deliberately
excluded from pooling until venue/asset unit normalization is explicit.

The report records input file hashes, split/embargo, block-bootstrap policy and
seed, each frozen model, validation/test diagnostics and `economic_evidence =
NOT_PROVEN`. No model can promote itself or obtain execution authority.

## Signal-to-PAPER-arrival latency evidence

A read-only stage report now separates strategy wait, coordinator latency,
final book revalidation and the synthetic ledger timestamp convention. It never
creates `exchange_ack` or real fill timestamps. Percentiles require minimum
sample sizes (p99 >=100, p99.9 >=1000); otherwise they are null.

On the frozen BTC ledger snapshot with 14 PAPER fills and 24 recorded candidate
events, descriptive same-recorder wall-clock evidence showed:

- candidate -> coordinator decision: median 3.7515 ms; p90 6.8629 ms;
- coordinator -> PAPER arrival decision: median 45.321 ms; p90 56.6011 ms;
- legacy `signal_age_ms_at_fill` capture -> PAPER arrival decision: median
  40.820 ms; p90 52.3646 ms;
- book receive -> PAPER decision: median 0 ms; p90 1 ms;
- ledger recording offset: exactly +1 ms in all 14 rows and excluded from all
  latency claims.

The 40-50 ms post-signal-age interval is consistent with the legacy final book
revalidation path and is the material internal target, not the ~3-7 ms
coordinator stage. `signal -> candidate` is NOT pure processing latency because
the frozen protocol may retain a valid signal while waiting for TTE eligibility.
Host/boot identity is absent from these legacy wall-clock records, so the report
explicitly refuses to call these numbers cross-host one-way latency.

## Native in-memory PM-book bridge

The V7 C++ stack already had both sides needed for the hot path: `MarketWsShard`
maintains a causal L10 `BookHotSnapshot` in RAM, while `simulate_taker_paper`
performs partial FAK against an `AggressiveBook`. The isolated branch now adds
only the missing allocation-free bridge `aggressive_book_from_hot`.

The bridge requires valid continuous lineage, positive receive monotonic time,
ordered on-book asks and positive size. It copies the canonical L10 fixed-point
ladder directly into the existing PAPER simulator. Invalid lineage or malformed
levels fail closed. No REST request, filesystem access or heap allocation is
introduced by this bridge. Existing external-execution tests now verify that
the in-RAM book produces the same FAK result as the explicit arrival-book fixture.

## Flat-account global checkpoint

A fail-closed checkpoint builder now joins the single allocation manifest,
external-fair PAPER account, frozen lead-lag state, structural state and the
validated canonical ledger. The initial shared-reservation checkpoint is
emitted only while every pre-existing lane is flat. Any unknown/open legacy
exposure, pending maker order, mixed SHA, cash identity mismatch or divergence
between component PnL, canonical economics and FINAL ledger PnL blocks it.

A read-only snapshot of the current flat PAPER account reconciled:

- account starting capital: 14000;
- canonical/ledger terminal PnL: 20.86413;
- external-fair cash: 4003.4;
- frozen lead-lag realized cash adjustment: 17.46413;
- structural cash: 2000;
- reserve cash: 8000.000000000001;
- global available PAPER cash: 14020.864130000000997.

Checkpoint id: `f713685d5ea15adc987084168add31bca223bd5fceb683bb800f4780e1561368`.
The checkpoint itself has zero entry authority. `ReservationProjection` can now
consume a hash-verified checkpoint while keeping source checkpoint SHA and new
writer/runtime SHA distinct; a cutover never relabels old accounting evidence
as if it had been generated by the new binary. Any subsequent foreign monetary
event still invalidates the checkpoint until reconciliation is rebuilt.

## Durable single-writer IPC acknowledgement

The reservation path is now connected to the existing `ledger_router` rather
than a direct-writer test callback. The router can optionally expose a bounded
Unix-stream request socket while remaining the only process that opens the
canonical ledger. Its main writer thread validates authority, appends and fsyncs
before returning a `durable=true` ACK. File-spool draining remains available on
the slower existing schedule; IPC polling is independently bounded to 1 ms by
default when explicitly enabled.

Reservation events now propagate the exact coordinator receipt at the canonical
metadata level. The firewall additionally binds `selected_replay_key` to the
event candidate/opportunity or reservation request. A nonempty but unrelated
receipt can no longer pass. Duplicate identical record IDs return an idempotent
durable ACK; the same record ID with different bytes fails closed. Invalid
authority is quarantined and never ACKed durable.

An integration test runs separate coordinator and ledger-router threads:
reconciled checkpoint -> candidate IPC -> coordinator reservation -> ledger IPC
-> sole CanonicalLedgerWriter -> fsync ACK -> coordinator response. The test
produces exactly one canonical `CAPITAL_RESERVE` and no receipt file. The IPC
socket is not enabled in the frozen production process manifest.

## Frozen multi-crypto PAPER-forward contract

A new typed `PAPER_MULTI_CRYPTO_FORWARD` envelope is now distinct from the
legacy frozen BTC `PAPER_FORWARD_TEST`. It carries immutable experiment,
protocol, feature-schema, model, fill-model, cost-model, settlement-semantic and
latency-profile identities plus asset/horizon. It is valid only for the six
registered crypto assets and M5/M15, remains `research_only=true`, has no
automatic promotion and uses PM only as the entry prior rather than an invented
absolute settlement fair.

The same packet is checked independently by the opportunity parser, the global
coordinator reservation layer and the canonical ledger authority firewall.
Non-BTC/M15 PAPER exploration without this packet is rejected. A multi-crypto
risk-creating ledger event must carry the exact packet and exact replay key from
the coordinator receipt; changing the protocol packet sends the event to
quarantine. The generic JSON schema now also represents all typed optional
opportunity surfaces and includes DOGE/BNB in the shared crypto context.

Parameterized contract tests cover BTC/ETH/SOL/XRP/DOGE/BNB across M5/M15,
invalid lineage hashes, settlement mismatch, invalid latency profile, disabled
PAPER gate, experiment mismatch and ledger packet tampering. No runtime manifest
or active lane is changed by this contract.

## Canonical replay-key lineage hardening

The full exact-SHA verifier exposed a real regression after the ledger firewall
was tightened: historical PAPER router fixtures (and the corresponding router
path) kept the coordinator receipt but did not propagate the selected replay key
onto canonical ORDER/FILL/terminal events. A nonempty but unrelated candidate
id therefore could no longer prove that the receipt authorized that event.

The firewall was not relaxed. Instead the router now writes the selected
`replay_key` into `opportunity_id` on canonical `ORDER_SUBMITTED` and `FILL`, and
recovery `NONFILL` plus reconciled `FINAL` inherit that exact opportunity id.
Tests were corrected to model the same lineage. This makes authorization
identity continuous across candidate -> order -> fill -> recovery/final.

## Prospective protocol-freeze builder

`v7_multi_crypto_forward_freeze.py` now creates one immutable multi-crypto
PAPER-forward protocol only from an explicit preselected draft. It does not tune
or inspect forward outcomes. The draft must freeze exact source/model/fill/cost/
settlement/latency hashes, common train/validation/data cutoffs and embargo,
entry threshold/TTE/signal age/size/no-chase/FAK semantics, shared risk caps,
primary endpoint, cluster/bootstrap policy, fixed duration and a PnL-independent
stopping rule.

Rules/token/oracle/feed/book/fee/fill/latency/accounting/single-writer/training
artifact evidence must all already be verified; any false flag blocks freezing.
Changing an economic/statistical parameter changes the protocol hash. The
result embeds the same `PAPER_MULTI_CRYPTO_FORWARD` packet consumed by the typed
opportunity contract and still sets `entry_authority=false` and
`automatic_promotion=false`. No thresholds are invented by the builder.

## Prospective multi-crypto forward report

`v7_multi_crypto_forward_report.py` reads one frozen protocol plus canonical
ledger events for that exact code SHA. The first denominator is durable
`CAPITAL_RESERVE`, then submitted/fill/resolved counts are reported separately.
A market with a fill but no FINAL is `FILLED_PENDING_SETTLEMENT`; its PnL remains
missing and therefore the cohort `total_pnl` is null until all authorized
markets are terminal.

The reporter rejects multiple entries in one market, duplicate FINALs and any
event whose `multi_crypto_forward` packet differs from the frozen manifest or
coordinator receipt. It includes resolved-PnL contribution, per-authorized-market
PnL only when complete, shared-shock/time cluster bootstrap, best-one/best-three
concentration checks and descriptive 25-market chronological blocks. Annualized
Sharpe is deliberately not computed. No report output grants execution authority.

## Native authorized multi-crypto taker PAPER executor

The existing C++ external-execution library now includes a mechanical
`execute_authorized_multi_crypto_taker_paper` step. It does not decide whether
to trade. It requires an already-durable PAPER authorization/reservation bound
to the exact `ExecutionPlan` intent/instrument/model/policy identity, an expiry,
a minimum state version, a maximum PM-book age and a maximum debit.

At simulated arrival it consumes the current causal `BookHotSnapshot` directly,
requires valid continuous lineage and a sufficiently new state version, converts
that in-RAM L10 ladder through the existing `aggressive_book_from_hot`, then
calls the existing partial-FAK simulator. The resulting gross book cost plus
authoritative fee must stay inside the durable reservation; otherwise the
mechanical result is rejected. No live order or wallet path exists.

Native tests cover valid fill plus intent mismatch, authorization expiry,
reservation overrun, stale book, stale state generation and broken book lineage.
The function remains pure and trivially-copyable at its typed boundaries; it is
not yet activated by the production process manifest.
