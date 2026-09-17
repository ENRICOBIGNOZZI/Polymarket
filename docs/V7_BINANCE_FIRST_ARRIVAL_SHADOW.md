# Binance aggTrade first-arrival shadow

Purpose: test whether redundant public Binance connections can reduce upstream causal latency without changing the canonical trading path.

The candidate consumes normalized `ExternalVenueEvent` trades only when:
- venue is Binance Spot;
- event type is Trade;
- aggregate trade sequence is nonzero;
- price/size/side and receive timestamp are valid;
- the event is healthy, non-stale and gap-free.

The first copy of a new aggregate-trade sequence is emitted only to the SHADOW consumer. Later copies never create a second shadow event.

Independent copies with identical economic payload confirm the first copy. Same-lane repeats are counted separately and do not count as independent confirmation. Same sequence with different price, size, side, asset or exchange time is a conflict.The gate is fixed-capacity and allocation-free. It keeps a monotone sequence high-watermark so an old copy that is no longer resident cannot be re-emitted after a direct-map collision. A delayed confirming copy is still accepted while its exact sequence remains resident.

Callers must preserve the repository's single-owner causal merge: per-connection producers feed bounded queues and the owner processes available heads by local monotonic receive time. The gate is not designed for concurrent callback invocation.

Development mechanism benchmark on the contended Mac, 5,000,000 first+confirm pairs:
- compact gate: about 32.7 ns per observation;
- the earlier full-event slot prototype was about 138 ns per observation.

These numbers are host-side compute only. The motivating read-only evidence is PR #1110: a 60-second native capture saw 155/155 BTCUSDT aggTrade identities match across three Binance public connections with zero conflicts; virtual first-of-three saved 5.470 ms p95 and 19.435 ms p99 versus the 9443 lane in that window.

No strategy, threshold, risk, OMS, order submission or execution authority is changed. Promotion requires exact-SHA London SHADOW evidence, zero silent drops/conflicts, and explicit accounting for stale/reordered sequences.