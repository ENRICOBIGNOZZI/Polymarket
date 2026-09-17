# V7 causal-to-wire latency contract

The speed objective is measured from the **local receive time of the causal market event**, not from a later polling tick, signal grid, candidate file, or decision timestamp.

Canonical stages:

`causal event RX -> signal ready -> decision -> OMS queue -> wire send -> venue ACK`

All stage timestamps in this contract are from the same local monotonic clock domain. Exchange timestamps are evidence about the venue event; they are not substituted for local one-way latency.

## Why the causal timestamp matters

A strategy can compute in microseconds after a signal becomes available and still be slow if signal construction waits for a 5 ms poll or a 25 ms grid. Measuring only `signal -> decision` hides that delay. `StrategyIntent::causal_trigger_receive_monotonic_ns` carries the original local event receive time forward into the OMS. `signal_ready_monotonic_ns` exposes deliberate quantization separately.

The fields are optional. Zero means unavailable. Reporting code must never translate a missing stage into a zero-latency stage.

## OMS measurement

`oms_latency_snapshot()` reports only legs whose timestamps are present and monotone:

- trigger -> signal
- signal -> decision
- trigger -> decision
- decision -> queue
- queue -> wire
- wire -> ACK
- trigger -> wire
- trigger -> ACK

Each valid leg has a bit in `valid_mask`. A missing or time-reversed stage remains invalid. End-to-end `trigger -> wire/ACK` is reported only when the full intermediate causal chain is monotone; valid endpoints never excuse a broken middle stage.

## Promotion metric

For latency work, the primary local compute/transport metric is `causal trigger RX -> wire send`. When authenticated PAPER/real execution is separately authorized and measurable, `causal trigger RX -> venue ACK` is the external end-to-end metric.

Always report p50, p95, p99, p99.9, max, sample count, drops/gaps and CPU/load context. Never infer exchange latency from synthetic fill timestamps or clocks on different hosts.
