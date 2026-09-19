# V7 multi-rate native migration

## Scope and authority

One canonical economic engine and bounded native worker partitions remain.
This migration changes temporal plumbing and removes parallel obsolete runtimes.
It does not enable real orders, authenticated execution, larger budgets or new
asset exclusions. Existing native risk, capital, inventory and OMS checks remain.

Integration starts at main `28588f621dcbb72b6fef3ec4cdaa8b97ba499c9e` and includes
PR 1194 at `12da99f3a3a29cea30427b3f38ad0c11ffa354da` and the writer-clock correction
from PR 1195. The probability/EV candidate remains explicit opt-in PAPER research.

## Fast path

Binance/Coinbase and Polymarket events update native state. The single owner
consumes at most eight context messages without blocking; no file read, JSON
parse, Python call, network inference, or wait for a slower venue occurs there.
Book-lineage faults cancel first. A fresh external shock can cancel the exposed
maker sides without waiting for Chainlink or another Polymarket tick. A candidate
cannot recreate the same toxic maker exposure in that cut.

Required slow fields are a per-policy mask. Missing/expired required context
rejects that dependent candidate; an independent fast policy has mask zero.
Risk-off does not depend on slow-model availability. This is not permission to
replace a required settlement oracle with an exchange price.

## Slow context

The existing cold manager publishes market/run/SHA/asset/horizon-bound snapshots.
A reader thread parses bounded JSON outside decision CPUs (control CPU affinity
on Linux; utility QoS on macOS) and transfers POD data through a bounded SPSC ring.
Overflow or malformed context invalidates availability. Failures are observable.
No secondary runtime process, order owner, allocator, or ledger is introduced.

Every field retains source receive time, source version and expiry. Publishing
again does not refresh its source age. Funding updates cannot rejuvenate old OI.
The source's conservative book clocks, not a JSON file timestamp, gate composite
features. Missing history remains null. Derivative OI retains venue-native units.
The currently available RTDS reference is bound only to BTC/M5 and its exact
market. Other asset/horizon oracle fields remain unavailable rather than borrowed.
Historical binary tape layout and existing ledgers are unchanged.

## Removed runtime surfaces

The Python external-fair router class, Python lead-lag execution loop, Python
portfolio coordinator, separate authorized-maker executable and duplicate native
candidate executable are removed. There is one native executable/source name.
Historical fair-value/accounting/index functions survive in
`scripts/v7_external_fair_research.py`; frozen rule/replay helpers survive in
`scripts/v7_lead_lag_policy.py`. They do not launch alternative execution loops.
Old producer names in historical evidence catalogs remain provenance, not a
claim that those producers are still running. Git history retains old programs.

Tests of deleted executable entrypoints are retired; historical ledger,
reservation, reconciliation, journal and replay tests remain. New native tests
cover per-field expiration, stale/future/identity rejection, queue overflow,
concurrent handoff, derivative clock independence and protective quote sides.

## Model and measurement boundary

The new 13-field context is observable but NOT consumed by the current frozen
18-feature probability candidate; evidence explicitly records `model_used_mask=0`.
Fitting a slow-prior/fast-residual model requires newly joined causal observations
and prospective validation. No invented coefficients or automatic fallback exist.
The established 5-second economic signal-age rule is not silently replaced by
the source's 100ms technical cancellation TTL. Existing signal-age replay/report
utilities remain available for a separately identified expiry experiment.

Multiple incompatible alpha candidates still fail closed until scores are on a
validated common economic scale. This migration does not claim a learned optimal
Q controller, positive expected profit, measured exchange latency or production
cutover. Compiler/runtime tests are distinct from exact-SHA London qualification.
