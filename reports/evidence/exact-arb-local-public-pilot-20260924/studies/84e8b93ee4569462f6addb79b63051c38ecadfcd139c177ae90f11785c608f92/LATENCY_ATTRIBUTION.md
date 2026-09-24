# Latency attribution

Study: `3b5a3d2787f463f729a1c1bf14b2173ec6c868cba9138f6eaca92697a4c0dd3c`. Recorded model SHA: `f50147e18e02ebed76cabe900b708e4d9479e603`.

Session: 1790267378940-30808. PAPER / zero authority.

Automatically generated from this directory's `report.json` and hash-bound evidence. This is a recorded-session analysis, not current London health, full CI certification or a profitability claim. Missing data means UNVERIFIED, never zero.

Population: UNIQUE_RELATION_OBSERVATIONS_WITH_MATCHING_RAW_FRAME. Distinct frames: 52; paired relation observations: 130; missing raw-frame joins: 0.

| Recorded stage | Samples | p10 | p50 | p90 | p95 | p99 | p99.9 | max | Fraction of paired elapsed total |
| --- | --- | --- | --- | --- | --- | --- | --- | --- | --- |
| receive_to_decode_ns | 130 | 43875 | 68166 | 897666 | 897666 | 897666 | 897666 | 897666 | 5254608/19136519 |
| decode_to_graph_start_ns | 130 | 6542 | 12542 | 1103250 | 1103250 | 1103250 | 1103250 | 1103250 | 16106458/57409557 |
| graph_start_to_relation_emit_ns | 130 | 19541 | 39750 | 1695959 | 1710417 | 1721667 | 1724959 | 1727209 | 25539275/57409557 |
| receive_to_relation_emit_ns | 130 | 69459 | 122666 | 3696875 | 3711333 | 3722583 | 3725875 | 3728125 | UNVERIFIED |

All quantiles are nanoseconds. sorted_values[floor(p*(n-1))].

sum(stage_ns)/sum(end_to_end_ns) on identical paired observations; not sum of quantiles.

- Receive-to-decode includes native parsing and book processing; these are not separately timed.
- Graph start is frame-wide; emit elapsed includes preceding relations and telemetry work.
- Decode/dispatch samples repeat for relations from the same frame; not independent frame samples.
- Missing observations and unknown producer tail can bias the observed latency distribution.
- Scenario arrival delays are assumptions, not measured transport latency.

| Unmeasured segment | Status |
| --- | --- |
| external_feed_to_host | UNVERIFIED |
| parser_vs_local_book_update | UNVERIFIED |
| isolated_relation_compute | UNVERIFIED |
| strategy_to_risk | UNVERIFIED |
| risk_to_serialization | UNVERIFIED |
| serialization_to_socket | UNVERIFIED |
| socket_to_exchange | UNVERIFIED |
| exchange_to_ack | UNVERIFIED |
| ack_to_fill | UNVERIFIED |

Transport, per-token venue holds and response sensitivity arms are in ECONOMIC_FUNNEL.md. Their assumed delays are not measured London transport times, contemporaneous venue attestations or a verified latency-value curve.
