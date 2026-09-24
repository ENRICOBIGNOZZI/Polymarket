# Causal shadow capital lifecycle — not verified venue capacity

Branch `research/unified-exact-arb-graph`; base HEAD
`f50147e18e02ebed76cabe900b708e4d9479e603` plus local uncommitted changes.
PAPER / SHADOW only. No orders, live capital, authority changes, promotion,
merge or deployment. Frozen champion economics are unchanged.

## Implemented

`scripts/v7_exact_arb_resource_scheduler.py` now has an opt-in
`ShadowCapitalJournal` alongside the unchanged-policy no-release planning
baseline. Each capital/latency scenario has its own persistent world identity,
initial cash/inventory, reservations, exact rational balance deltas and result
receipts. A journal cannot silently change between baseline and lifecycle mode.

The native study runner interleaves decision-time allocation and shared N-leg
execution events. Earlier modeled results can fund later decisions in the same
world; another latency arm's outcome or a future result cannot. Release becomes
available strictly after the modeled result (one nanosecond tie breaker), not
at an assumed venue settlement instant. Shared depth debits survive cash release;
released cash does not manufacture fresh displayed liquidity.

For closed outcomes the journal recomputes cash, inventory, declared-model fees
and flat unwind PnL from each fill, rather than trusting a reported profit:

| Outcome | Accounting |
| --- | --- |
| No legs filled | Release the unused funding after all modeled ACKs |
| All legs filled, BUY | Deduct acquisition cash/fees; retain tokens and reserve; no payout credit |
| All legs filled, SELL | Consume explicitly prefunded inventory; add net cash; no synthetic short |
| Partial fills fully unwound | Account entry and unwind cash/fees/loss; retain reserve |
| Censored, residual exposure or unverified transformation | Retain the complete original hold; no guessed release |

Entry/unwind timestamps, quantities, source-candidate hashes, world identity,
fee bounds and resource bounds are checked transactionally. Unwind cannot
precede knowledge of entry results. Duplicate outcomes are idempotent;
conflicting replay fails. Persistence failure rolls back both balances and
holds. Restart preserves reservations and outcomes. A receipt reads one SQLite
snapshot and distinguishes pending orders from censored outcomes.

New immutable, hashed study artifacts are `capital_plans.jsonl` and
`capital_transitions.jsonl`. Existing `resource_plans.jsonl` remains the
independent no-release diagnostic baseline. Per-world report receipts expose
modeled balances, encumbrances and known flat unwind PnL separately from unknown
portfolio PnL. Capital-time is explicitly **reservation-to-result only**, not
the total inventory/settlement lock duration.

## Integrated causal fixture

A new synthetic study starts with 4.2 PUSD and two opportunities. The first
attempt fills nothing. With a zero-delay modeled ACK, its funding is known free
before the second decision: two order groups close, leaving 0.1 PUSD and five
tokens of each binary outcome. With a five-millisecond ACK delay, the second
decision cannot spend the still-encumbered funds: only one group closes and
cash remains 4.2 PUSD. The independent no-release baseline admits only one.

This runs both through Python replay fixtures and through the actual C++ WS
decoder → causal study runner. Repeating the input reproduces the immutable
report. The acquired tokens are not credited as collateral or realized profit.

## Validation

- Full local Python suite: **2,749 passed**, one existing skip, 384 numerical warnings.
- Full configured Release CTest: **415/415 passed**.
- Final focused scheduler/runner/report suite: **98 passed** after the final
  atomic-receipt and pending/censored accounting changes.
- Four scoped native/integration tests pass in Debug, combined ASan/UBSan and
  TSan, including the strengthened sizing oracle and native decoder roundtrip.
  Native C++ was unchanged in this increment; these used existing build trees.
  This does not sanitize Python or certify a clean Linux release.
- `git diff --check` passes. Champion lane, economics and multi-engine files
  have no diff. These are local gates, not official CI/release approval.

## Still open — no SOTA or profitability claim

This is a conditional public-L2 fill model, not observed venue balances,
execution, ACK timing or settlement. Fee aggregation/rounding remains
unverified; the lifecycle uses the explicitly declared research fee model.
Inventory acquired by full BUY baskets remains locked without independent
merge/redeem/settlement evidence. No capital expiry invents a payout. Reserve
is an encumbrance, not a claimed venue fee. SELL fixtures have prefunded model
inventory, not an independently verified live inventory source.

Fixed-size admission is not a capital-dependent optimal sizing/capacity curve.
Maker paired-fill/queue/adverse-selection calibration, full inventory capital
time, worst-case full-depth load, same-host champion latency non-regression,
hourly multi-session aggregation and official CI/release remain incomplete.
The [fee attribution](exact-arb-fee-attribution-checkpoint-20260924.md) explains
all 122 differing quantities but does not close champion parity or venue fee
semantics. [NegRisk observation](exact-arb-negrisk-attestation-checkpoint-20260924.md)
does not authorize complete-set or conversion relations.

London causal PAPER exposure and economic stopping-rule evaluation are still
required. No deployment occurred; `research_decision = null`.
