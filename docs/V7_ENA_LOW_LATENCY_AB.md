# V7 ENA low-latency A/B

This experiment measures NIC-to-userspace latency changes on the dedicated
London PAPER benchmark hosts. It never authorizes orders or persists a host
tuning automatically.

The experiment is paired and interleaved. Before each candidate profile it
restores the original ENA coalescing, IRQ affinity, and irqbalance state, then
runs a fresh baseline against the same public Polymarket `/time` endpoint.

Profiles are intentionally separable:

- per-socket `SO_BUSY_POLL=50us`;
- ENA interrupt moderation disabled (`rx-usecs=0`, `tx-usecs=0`);
- ENA queue IRQs pinned only to the three feed CPUs, never the decision CPU;
- the combined treatment.

Every candidate records p50/p99/p99.9 total latency, first-byte tail latency,
failures, exact SHA, physical AZ identity, and applied host state. Tail
improvement is evidence only; it is not an automatic promotion decision.

The host script requires root because IRQ/coalescing changes and increasing
`SO_BUSY_POLL` require network-administration privilege on Linux. Production
promotion must separately prove a least-privilege mechanism for the runtime;
the benchmark does not grant the trading service `CAP_NET_ADMIN`.

Rollback is part of the measurement. A run is invalid unless the final
coalescing state, IRQ affinity map, and irqbalance state exactly match the
pre-run snapshot. Global `net.core.busy_read` and `net.core.busy_poll` values
are observed for provenance but are not mutated by this experiment.

Run locally on an ENA host with the PAPER runtime stopped using
`ops/v7_ena_latency_ab.py`. Run the same exact-SHA experiment across all three
physical London AZs through `ops/v7_london_ena_ab_ssm.py`.
