# V7 Monitoring

V7 exposes one monitoring plane and one exporter listener. The canonical manifest is `monitoring/v7_monitoring_manifest.json`.

Required views cover overview, maker/fillability, structural, external fair, informed taker, graph/RV, event lanes, slow models, capital, risk, execution, latency and model evolution.

Hard health failures include stale or discontinuous feeds, invalid semantics/reference, unknown fees, expired fair value, OMS/inventory mismatch, cancel failure, rate-limit exhaustion and drawdown breach.

The maker fillability surface distinguishes created, effective, rested, reachable, queue-depleted scenarios, eligible, partial and full fills. Exact-WS gaps and drops remain explicit.

The Grafana operator home separates authority from readiness and shows runtime
identity, single-writer proof, action funnel, data-source health, latency tails,
disk pressure and restart/quarantine state. Missing external-fair artifacts are
reported as not ready, never as a healthy P0 stack.
