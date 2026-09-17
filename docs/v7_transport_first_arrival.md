# V7 transport first-arrival and ingress wakeup

Base: `9579554f347277d273afe6ec9f79f7e6eaac6df2`.

## Changes

The existing external-data consumer sleeps for five milliseconds after each
iteration. The opt-in `--event-driven-ingress` mode instead waits for a
nonblocking Linux eventfd (a nonblocking pipe on macOS). A successfully enqueued
venue event wakes the same consumer. The original bounded per-connection SPSC
queues, receive-time batch sorting, gap handling and single state writer remain.
Signals may coalesce; events do not. A five-millisecond idle deadline retains
periodic health/expiry checks and bounded shutdown without busy spinning.

All six normalized ingress queues use the same wakeup. Producer notification
introduces no application mutex or allocation. Queue saturation still records
drops and propagates a gap; notification saturation only means a signal is
already pending. Notification errors are visible and retain the timed fallback.
The status surface reports the actual wait mode, idle deadline and errors. It
does not present event-driven operation as a zero-millisecond polling interval.

External WebSocket connections explicitly enable TCP_NODELAY for small outgoing
subscription/control frames. Existing persistent HTTP connection reuse is kept.
This is not a claim about incoming network latency or exchange acknowledgments.

## Reproducible measurements

`polymarket_v7_ingress_latency_bench --samples 2000 --rounds 3` compares the
original five-millisecond wait with event notification through the real bounded
ingress implementation. Mode order alternates by round; each paired round uses
the same seed. Results include p50/p95/p99, CPU time, exact event counts, dropped
events, sequence checks and notification errors. These measurements cover only
synthetic producer-to-consumer queue handoff, NOT decoding, network, inference,
order submission, fills or profit. Tail behavior remains host/load-dependent.

`polymarket_v7_feed_race_probe --duration-seconds 60` subscribes concurrently to
three documented Binance Spot public market-data endpoints: stream.binance.com
on ports 9443 and 443, and data-stream.binance.vision on 443. It captures BTC,
ETH and SOL bookTicker and aggregate-trade streams without credentials. The
same native transport is used for all endpoints. Recording starts after a
bounded readiness wait and warmup; endpoint failures remain explicit.

Compare only identical exchange identities and payload fields on the SAME
host. The report separates assets and stream types, counts unmatched and
conflicting messages, and preserves missing percentiles as null. Endpoint
penalties and savings are conditional on all three endpoints carrying the
same message; coverage must be read alongside them. No exchange timestamp is
subtracted from a local timestamp. No claim about Binance's physical location
or cross-host one-way latency can follow from this test. Capture memory and
duration are bounded; truncation or transport failures invalidate clean capture.

Official endpoint and stream specifications were checked on 2026-09-17 in
Binance Developer Documentation, Spot WebSocket Market Streams, and Binance's
How to Use Binance Websocket Stream documentation. No undocumented gateway,
Cloudflare bypass, FIX entitlement or SBE credential is assumed.

## Deployment and research boundaries

The production launcher is unchanged: event-driven ingress is OFF by default.
An isolated London observer may run it with an output path outside the canonical
run. It creates no execution owner, writer, orders or ledger. Faster wakeups can
change the live grouping/timing of events, so activation requires a distinct
prospective cohort rather than relabeling or modifying the frozen BTC study.
The existing full-status JSON publishing path is not eliminated by this patch.
This is a handoff improvement, not an end-to-end low-latency certification.

Do not join redundant sockets directly to one SPSC queue. A production feed
race requires separate queues, logical source epochs, exact duplicate handling,
health/recovery behavior and causal execution tests. This diagnostic probe is
not authorization to enable that policy. Geographic relays are likewise not
introduced without measured same-event arrival improvement at London.

Validation: deterministic wakeup tests cover pre-wait signals, saturation,
concurrent notifications and 1000 exactly delivered queue events. Probe reducer
tests cover exact identities, duplicate copies, conflicts and missing data.
London exact-head build/test/benchmark receipts are required before integration.
