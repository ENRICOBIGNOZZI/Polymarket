# V7 Latency Engineering

V7 has an HFT-oriented architecture. That is not evidence that the deployed path is HFT-fast. Latency claims are split into three non-substitutable layers:

1. synthetic exact-binary internal compute;
2. representative public-feed replay and forward PAPER measurement;
3. geographically distributed network/CLOB request, ACK, cancel and user-WS evidence.

Only layer 1 is enforced in CI today. Layer 2 requires an exact-head forward artifact. Layer 3 has not been proven and authenticated order/cancel probes are blocked while execution authority remains disabled.

## Historical baseline

An older, noncanonical Fast forward run reported 545.7 ms end-to-end p99, 524.0 ms feed p99, 39.1 ms decision p99 and 233.6 ms p10 opportunity lifetime. It is only a regression baseline; it is not current V7 evidence.

## Current instrumentation

The C++ pipeline records monotonic nanosecond durations for JSON parse, book application, features, decision, inline risk, order-TX queue, PAPER execution and receive-to-intent. Telemetry crosses a bounded SPSC queue before filesystem output, so the decision owner does not write logs.

The release benchmark covers synthetic WS bytes through canonical L2 and maker intent. `scripts/v7_latency_gate.py` enforces the internal thresholds in `config/v7_latency_slo.json`. Its output explicitly states that it contains no network/CLOB or representative venue proof.

## Persistent transport

`HttpClient` owns one libcurl easy handle for its lifetime. Requests reset options without destroying the handle, retaining libcurl connection, DNS and TLS session caches. TCP keepalive and HTTP/2-over-TLS negotiation are enabled. The handle is serialized for a dedicated I/O owner and is never called from the market-data or maker-decision path.

This removes per-request `curl_easy_init`/`curl_easy_cleanup`. It does not prove that an authenticated order path is safe or fast. Connection prewarming, signing, request-to-ACK and user-WS confirmation remain mandatory before any future authenticated authority.

## Regional shootout

`polymarket_v7_latency_probe` performs read-only repeated GETs against an HTTPS Polymarket endpoint and reports DNS, TCP, TLS, first-byte and total p50/p90/p95/p99/p99.9/max plus connection reuse. Run the same exact SHA and configuration for 24 hours in Frankfurt, London, Amsterdam, New York and Northern Virginia.

The public probe cannot select a production node by itself. Final selection also requires request-to-ACK, cancel-to-ACK, user-WS confirmation, reconnect and loss measurements. Those authenticated measurements stay blocked until the operator explicitly grants that authority.

## Venue-aware policy

Applicable crypto takers can enter a 250 ms delay while resting orders remain cancelable. Maker toxic-quote cancellation is therefore the critical latency objective. Per-signer order and cancel token buckets make blind cancel/repost loops both wasteful and potentially rate-limited; V7 preserves queue priority when a quote remains economic and lets critical toxicity cancels override dwell.
