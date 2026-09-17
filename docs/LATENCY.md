# V7 Latency Engineering

V7 has an HFT-oriented architecture. That is not evidence that the deployed path is HFT-fast. Latency claims are split into three non-substitutable layers:

1. synthetic exact-binary internal compute;
2. representative public-feed replay and forward PAPER measurement;
3. geographically distributed network/CLOB request, ACK, cancel and user-WS evidence.

Latency is measured separately for feed arrival, local feature/inference work, coordinator/executor work and PAPER decision-to-arrival simulation. Public-feed and PAPER measurements are not evidence about a real-money order path.

## Historical baseline

An older, noncanonical Fast forward run reported 545.7 ms end-to-end p99, 524.0 ms feed p99, 39.1 ms decision p99 and 233.6 ms p10 opportunity lifetime. It is only a regression baseline; it is not current V7 evidence.

## Current instrumentation

The crypto/maker pipeline records monotonic nanosecond durations for JSON parse, book application, features, decision, inline risk, order-TX queue, PAPER execution and receive-to-intent where those stages exist. Telemetry is kept off the decision critical section where possible.

The Prometheus exporter reads the active professional-maker latency stream and exposes only stages produced by the current crypto runtime. Missing files are reported as missing evidence; deleted strategy streams are never substituted for current measurements.

The release benchmark covers synthetic WS bytes through canonical L2 and maker intent. `scripts/v7_latency_gate.py` enforces the internal thresholds in `config/v7_latency_slo.json`. Its output explicitly states that it contains no network/CLOB or representative venue proof.

## Persistent transport

`HttpClient` owns one libcurl easy handle for its lifetime. Requests reset options without destroying the handle, retaining libcurl connection, DNS and TLS session caches. TCP keepalive and HTTP/2-over-TLS negotiation are enabled. The handle is serialized for a dedicated I/O owner and is never called from the market-data or maker-decision path.

This removes per-request `curl_easy_init`/`curl_easy_cleanup`. It is an implementation optimization for the public-data/PAPER path.

## Regional shootout

`polymarket_v7_latency_probe` performs read-only repeated GETs against the public `https://clob.polymarket.com/time` endpoint and reports DNS, TCP, TLS, first-byte and total p50/p90/p95/p99/p99.9/max plus connection reuse, failures and reconnects. It is a public HTTPS connectivity probe only: it does not measure authenticated order/cancel ACK, signal-to-send, or signal-to-fill latency. Run the same exact SHA and configuration for 24 hours when comparing regions:

```text
polymarket_v7_latency_probe --region Frankfurt --exact-code-sha <40-lowercase-hex-sha>
python3 scripts/v7_regional_shootout.py --policy config/v7_latency_slo.json \
  --probe frankfurt.json --probe london.json --probe amsterdam.json \
  --probe new-york.json --probe northern-virginia.json
```

The evaluator rejects a mixed SHA, a missing candidate region, short collection
window, inadequate samples, excessive failure/reconnect rate or malformed
percentile distribution. It ranks healthy regions by p99.9 then p99 total
latency, but its output is still read-only evidence and never authorizes live
execution.

The public probe is used only to characterize data-path latency and regional stability for the PAPER research system.


## Venue-aware policy

Applicable crypto takers can enter a 250 ms delay while resting orders remain cancelable. Maker toxic-quote cancellation is therefore the critical latency objective. Blind cancel/repost loops destroy queue priority; V7 preserves a resting quote while it remains economic and lets critical toxicity cancels override dwell.

## London AZ shootout

The London migration uses the same public probe but keys candidates by physical AZ ID. `config/v7_london_az_shootout.json` records the Polymarket-provided mapping for `eu-west-2a/b/c`; each EC2 host must verify both its AZ name and AZ ID with IMDSv2 before evidence is accepted.

Run `ops/v7_london_benchmark.sh smoke` only as a wiring check. Run `ops/v7_london_benchmark.sh formal` on all three candidate hosts for formal evidence, then evaluate the three probe JSON files with `scripts/v7_regional_shootout.py --candidate-region euw2-az1 --candidate-region euw2-az2 --candidate-region euw2-az3`.

The regional evaluator is read-only and fail-closed. It requires the same exact SHA, full duration/sample gates, bounded failures/reconnects and monotone percentile distributions. It does not measure authenticated order/cancel ACK and cannot authorize a cutover.

WebSocket evidence must keep connection health/freshness/jitter separate from one-way latency. Cross-host or exchange-to-host one-way latency remains unknown unless the clocks and source timestamp semantics make the subtraction valid.
