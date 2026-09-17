# V7 Multi-Crypto — Incident / Correction Log

This log records discovered integrity defects and their treatment. It is not an
attempt to erase old evidence. Historical records and failed CI runs remain part
of the audit trail.

## 2026-09-17 — Crypto engine state PnL omitted lead-lag component

**Observed.** Canonical economics grouped external-fair and lead-lag under the
same `CRYPTO_SETTLEMENT_ENGINE`, but the monitoring state comparison read only
the external-fair router. A canonical crypto-engine total was therefore being
compared against one component and reported as a divergence.

**Correction.** The isolated PR aggregates explicit external-fair + lead-lag
state components. If canonical economics requires lead-lag and the state is
missing/unsafe, reconciliation is `unverifiable`, never an implicit zero.
Production has not been rewritten by this branch.

## 2026-09-17 — Shared portfolio guard omitted lead-lag exposure

**Observed.** The account-level risk guard also read only external-fair equity
for the crypto engine, so lead-lag realized PnL/open cost-at-risk was absent from
that state-side engine view.

**Correction.** The isolated PR reconciles lead-lag status/state. Settled PnL is
included once. Open lead-lag cost+fee is conservatively marked at zero recovery
value for risk only until official settlement. This mark is not a FINAL and is
never reused as settlement evidence. Any state/count/PnL mismatch is fatal to
new portfolio risk.

## 2026-09-17 — Multi-crypto ShockTracker repeated-event non-idempotence

**Observed in parallel worktree.** Re-reading the same verified source
`state_version` produced a different normalized shock because the first read had
already updated variance. Reproduction: after two 0.0001 returns, the first
0.001 event produced z=10.0 and a repeated identical read produced ~3.620306.

**Correction prepared separately.** Commit
`8c4160b3aa44c74bce6cab25cb93c99ad49ea1c9` caches the exact output for a source
identity and rejects conflicting/older versions without mutating calibration.
It was intentionally not applied by this workstream to the parallel agent's
checkout. Until integrated there, this remains a known blocker for treating its
shock output as a stable frozen feature.

## 2026-09-17 — Coordinator receipt was not bound to canonical replay identity

**Observed.** The ledger firewall originally accepted a nonempty coordinator
receipt without proving that its `selected_replay_key` was the candidate/order
being written. Tightening this invariant exposed missing replay-key propagation
in the legacy PAPER router.

**Correction.** The firewall now binds the selected replay key to the canonical
candidate/opportunity or reservation request. The PAPER router propagates that
same key through ORDER_SUBMITTED, FILL, recovery NONFILL and FINAL. The firewall
was not relaxed.

## 2026-09-17 — Exact-SHA verification regression was preserved, not bypassed

**Observed.** The canonical verifier on SHA
`2d2dddc49de67b88d584f3708cde78ad91f94745` passed 192/193 tests and failed
`test_v7_external_fair_paper_router` after replay-key binding was hardened.

**Treatment.** The failed run remains valid negative evidence. CI/test rules
were not weakened. The root cause was fixed by propagating canonical replay
identity through the PAPER lifecycle, then the affected router/ledger tests were
rerun. Any verifier started before later source changes was explicitly treated
as stale and not cited as exact-SHA proof.

## 2026-09-17 — Legacy +1 ms PAPER fill ordering is not latency evidence

**Observed.** Frozen lead-lag fills use `recorded_ts_ms = decision_ms + 1` for
synthetic causal ordering. The current 16-position accounting audit flags all 16
such fills as synthetic ordering, not exchange fill latency.

**Treatment.** Latency reporting excludes this offset, does not invent an
exchange ACK/real fill timestamp and reports only same-recorder descriptive
stages when host/boot clock identity is unavailable.

## Current correction policy

No historical row is deleted or rewritten. Where authoritative new evidence can
support a correction, the intended mechanism is append-only with original
identity, reason, new evidence hash and correction identity. Missing settlement
remains `UNRESOLVED`. No production correction/deploy is performed by this
draft branch.

## 2026-09-17 — Native multi-crypto executor was declared but not implemented

**Observed.** Commit `e501f1d3293919a621e623fd7155c8153cc47b4f`
contained the public declaration and tests for the authorized multi-crypto PAPER
taker, but no implementation in `src/v7_external_execution.cpp`. The committed
test also referenced fields that do not exist in `BookHotSnapshot` and
`ExecutionPlan`. A clean native build reproduced the compiler failure.

**Correction.** The executor is now implemented against the actual typed API:
identity comes from `plan.intent`, state generation from
`plan.market_state_version`, and the causal book is checked only for fields it
owns. Authorization, expiry, book age, state generation, lineage and maximum
durable debit all fail closed before a PAPER fill is accepted.

## 2026-09-17 — Debug exact-SHA exposed a cold-start freshness race in maker test

**Observed.** Exact-SHA Release passed 198/198, while Debug failed
`test_v7_authorized_make_cancel_runtime`: the fixture refreshed observed maker
features before spawning the native executor, so cold Debug startup could age the
500 ms feature cut before the authorization was consumed.

**Correction.** The test now starts the zero-authority consumer first, waits for
its initial status, refreshes the observer/account evidence, and only then
publishes the coordinator authorization. Production freshness limits were not
relaxed. The corrected Debug path passed 20/20 repeated native lifecycle runs.
