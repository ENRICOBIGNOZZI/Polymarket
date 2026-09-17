# Autonomous speed-improvement policy

All performance work must be evidence-driven and validated on the London AWS environment before it is considered valid.

The speed loop may profile, benchmark and optimize transport, feed handling, parsing, publication, feature computation, inference, decision logic, serialization, IPC, logging and other hot-path work. It must preserve economic semantics, causality, PAPER-only authority, accounting correctness and single-writer guarantees.

Local/Mac results are development evidence only. Any claimed latency improvement must be reproduced on the authorized London host using the candidate exact SHA and a comparable workload. Report p50/p90/p99/max where meaningful, sample count, error/reconnect/drop counts, CPU/memory/load context and before/after exact SHAs. Separate network latency, feed freshness, compute/inference latency, decision-to-send latency and end-to-end decision-to-arrival latency.

Optimization work must use isolated branches, add regression/performance tests where practical, and never change thresholds, model decisions, risk limits or execution semantics merely to make latency numbers look better. No direct writes to the production runtime. Integration and deployment remain governed by the existing continuation/release flow.

If a candidate is faster locally but not faster in London, it is not accepted as a speed improvement. If measurement quality is insufficient, record INCONCLUSIVE rather than selecting a winner.
