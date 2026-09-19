# Economic causality repair — 19 September 2026

## Boundary
PAPER only. No new owner, ledger, live orders, risk limits or model parameters.
Canonical FINAL records remain unchanged. Projection rows are read-only views.

## Accounting
Join each native aggregate FINAL to its explicit fills. Attribute identity,
component, position and cash decomposition together. Count fees once. Reject
ambiguous context, non-reconciling payouts and unallocated terminal costs.
Accounting reconciliation does not establish execution or economic validity.

## Execution
The native taker adapter queues the existing admitted command until a declared
arrival time. Matching uses the previously consumed PM book, before processing
a later PM event. Unknown terms, future/stale books and continuity failures are
censored. Visible size is not replenished merely by a new message version.
Pending arrivals retain their reservation; shutdown censors and closes them.
The retained policy requires full size at the same limit price. This is NOT a
complete FAK exchange replica: partial fills and price improvement are excluded.
The offline replay supports separate full/partial-fill research scenarios.

Public CLOB itode is checked against the exact condition and both token IDs.
The response, observation time and normalized-content SHA256 are retained.
Mandatory delay and the 250 ms transport scenario are distinct. The latter is
an assumption, not an observed RTT. Unknown mandatory delay is never zero.

## Capture
Full native book/trade capture is scoped to BTC:M5; other contexts retain
low-volume decisions. A full-capture launch requires 20 GiB free disk space.
Fault observations invalidate replay books until recovery. Closed-capture
manifests and sequence continuity are required. Missing capture is not a nonfill.

## Verification and interpretation
Run `pytest tests/test_v7_economic_causality.py` and the existing canonical,
projection, rollover and native-contract tests. Build and run the native PAPER
execution and runtime-evidence CTest targets. Use the canonical clean-tree
verifier for release acceptance; a local test is not deployment evidence.

A public read from London at 2026-09-19T10:32:52Z returned itode=true for BTC:M5
market 4679897, with fee rate 0.07 and exponent 1. This observation applies to
that market and timestamp only. The capture does not authorize live trading.

The probabilistic model remains frozen. A calibrated forecast, held-out net
edge, capacity, exact exchange queue and private order latency are not proved
by these repairs. No automatic promotion or higher sizing is authorized.
