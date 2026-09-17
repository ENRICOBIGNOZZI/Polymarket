# Pre-warmed CLOB order lane selector

This is a zero-authority latency candidate for persistent CLOB order transport.

The candidate may keep 2–4 independent order TLS connections warm, but **one logical order is sent on exactly one lane**. It is not a hedged/duplicate-order mechanism.

A lane is eligible only when:

- its connection epoch is current;
- it has completed at least one application-level round trip;
- that round trip is still within the configured freshness window;
- no other order owns the lane.

Among eligible lanes, the selector chooses the lowest most-recent RTT, with newer evidence as the tie-breaker.

## Retry safety

The critical boundary is whether any request byte may have reached the peer.
- `Reserved`: no send has started. Disconnect/failure may safely release the order for another lane.
- `SentAmbiguous`: send has started. A transport failure keeps that `order_id` blocked globally; it cannot be retried on another lane until explicit reconciliation resolves whether the exchange saw it.
- stale connection epochs and stale lease generations fail closed.

This preserves the single-submit invariant while allowing connection diversity to be measured.

## Evidence boundary

Development-host selector compute, 5,000,000 acquire+cancel cycles across three healthy lanes: approximately 30.5 ns/cycle. This is only host compute.

There is **no claim** that recent RTT predicts the next `/order` latency, or that multiple connections beat one connection. Promotion requires same-host London A/B using public/zero-authority probes first, then prospective PAPER evidence with exact-SHA accounting of p50/p95/p99/p99.9, disconnects and ambiguous-send incidence.

No credentials, socket creation, order submission, strategy, sizing, risk, OMS authority or real-money setting is changed here.
