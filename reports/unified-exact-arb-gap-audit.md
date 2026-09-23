# Unified exact-arbitrage audit

Baseline: `1be49edd0cbd219eedd164cd76a0d01d1be87ab1`, feature branch only.
All work is zero-authority research. No production readiness or live profitability
is inferred from fixture tests. This matrix supersedes the earlier completion claim.

This first matrix records findings **before these worktree changes**. Updated
disposition and measured results follow below; no row implies deployment approval.

| Feature | Current implementation | Ready? | Missing work / finding | Coverage | Live evidence |
|---|---|---|---|---|---|
| Universe discovery | Canonical crypto Gamma collector | Partial | No partition attestations emitted; UP/DOWN alias mismatch | Synthetic only | Must collect actual snapshot |
| Equality proofs | Exact Fraction state sums | Partial | Proof checks algebra, not truth of supplied state vectors; bind source identities | Unit | None |
| Duplicate markets | Context/time/semantic grouping | Partial | Rules/condition identity must independently establish equivalence | Fixture | None |
| N-way / NegRisk | Explicit attested hyperedges | Partial | Event completeness cannot follow from subset membership flags | Fixture | None |
| Combo / RFQ | Legacy status provenance | No | Provenance alone does not integrate hedge bounds or transformations | Fixture | None |
| Merge/split/redeem | Transform metadata | No | Stage costs, expiry, failure, redeem semantics | Limited | None |
| Native evaluator | Fixed arrays, BUY sweep | No | SELL inventory, canonical rounding, capacity, overflow, gross naming bug | One parity case | None |
| Native compilation | Header handles | No | Control-plane serialization / validated loader / promotion eligibility | None | None |
| Benchmark | Python Fraction vs arithmetic subtraction | No | Actual champion/native 2/3/4/8/16-leg comparison | None | None |
| Shadow runtime | Incremental Python | No | SHA/hash validation; per-event repeated evaluation; forced lineage true; future timestamps | Small replay | None |
| Survival | Delayed index queue | No | Future observation used at earlier due time; generation handles can change | Limited | None |
| Execution | Binary wrapper | No | Generic causal N-leg fills, unwind, transformation stages | Binary only | None |
| Resources | Per-timestamp scalar reservations | No | Reservations release on every update; quantity resource vector and lock expiry required | Same timestamp | None |
| Observability | Exporter counters/distributions | Partial | Fraction strings rejected by float conversion; lifetime, tick distances, health/recovery | Rendering | None |
| Restart / hot swap | Reload JSON | No | No generation integrity validation or checkpoint | None | None |
| Champion | Separate frozen lane | Preserve | Broaden buy/sell fee/depth parity; do not change economics | SOTA suite | Historical binary-only |
| Build / deployment | Existing official pipeline | Pending | Release/debug/sanitizer and branch checks, then live proof; no merge/deploy authorized yet | Prior partial matrix | No London access established |

Priority: real universe diagnostic and safe identity derivation; native correctness
and honest benchmarks; causal generic simulator; runtime recovery/resources;
adversarial validation. Unfinished rows remain explicit blockers to SOTA status.

## Disposition after implementation

| Feature | Implemented in this worktree | Remaining gate |
|---|---|---|
| Actual universe | Read-only yield diagnostic; canonical CTF binary identity, UP/DOWN mapping, event/tick/minimum provenance | 66 binary relations only; no independently verified cross-market/Combo/NegRisk breadth in this sample |
| Proof/compiler | Exact statewise proofs, normalized economic identity, graph SHA and model validation, source universe identity, three dependency indexes | External attestations still require trusted source semantics; algebra cannot establish their truth |
| Native data plane | Off-path generated immutable arrays; bounded BUY/SELL evaluator; inventory/capital/depth/minimum/overflow guards | Not connected to a production native graph event worker; transformed/inequality relations excluded explicitly |
| Numeric accounting | Integer microshare sizing; exact integer rounded fees for nanorates and exponents 0–2; rational oracle parity | Higher native fee exponents excluded; monetary totals still double before microcurrency quantization |
| Generic execution | N-leg sequential/parallel/non-atomic batch, causal arrival books, depth, partial fills, unwind, transformation stage | Transport scenarios, not verified venue availability/order semantics; conversion success not observed |
| Resources | Quantity reservations for collateral/inventory/transformation capacity, persistent release timing | Real resource inventory and conversion attestations absent from local observation |
| Causality | Per-leg local book age, lineage/gap invalidation, delayed as-of survival, reference anchors plus deltas | Rolling bounded history; unavailable coverage censored |
| Recovery | Rotated inode cursor, replay-restored resources/counters, durable opportunity IDs, incompatible generation rejection | Operational recovery across retained historical generations needs a generation archive/checkpoint design |
| Telemetry | Exact Fraction parsing, near-distance/tick/bps distributions, lifetime censoring, survival detail, health and queue counters | Quantiles are bounded last-100k windows, not all-history estimates; maker fill probabilities intentionally unavailable |
| Champion | Economic implementation unchanged; BUY/SELL parity matrix and independent tests | Controlled same-host latency non-regression not established |
| Validation | Full Release/Debug builds; native ASan/UBSan; targeted Python/property/security tests; actual C++ benchmark | Broad suite has four independent environment/platform failures; London validation remains absent |

See [the measured research report](unified-exact-arb-research-report.md) for evidence
scope, exact numbers and promotion blockers. This is **not a merge candidate**.
