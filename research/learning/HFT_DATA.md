The HFT data path reuses the six external-venue collectors, the 30-market public
PM observer and native decision capture. It does not change model features,
families, selection, risk limits or activation authority.

`monitoring/v7_hft_windows.py` runs in the existing cold retention service. It
freezes complete native prefixes into immutable compressed training chunks,
including rejected decisions and repricing observations. An incremental SQLite
index records unique opportunities and unions overlapping −2s/+5s decision
windows. The source receive, exchange, monotonic and sequence fields remain
unchanged. Native decision state supplies the book observed by the strategy;
the public PM observer has its own connection lineage and receive timestamps.

Before rolling raw retirement, the worker copies exact PM/CEX records inside
those windows into compressed, hash-verified objects. Every source gets a proof
linking its hash to the replayable object. All five context population watermarks
must pass an external segment before it can be retired. Unknown formats,
unread prefixes, missing watermarks and unhealthy native captures remain pinned.
Window selection includes TTE, expired, weak, EV and already-repriced rejects;
the runtime also records a no-change control at least every 30 seconds while
evaluating. The writer remains asynchronous and bounded. Missing coverage is
not reconstructed or converted into an assumed fill. Raw L2 deltas still need
a preceding venue snapshot for full-depth replay; normalized L1 events are
self-contained.

The user-authorized history reset is fixed at 2026-09-20 16:30 UTC. This is an
epoch boundary, not a daily rolling filter. Subsequent compact history and
opportunity windows are permanent. General raw retention is based on measured
growth, verified per-source compression and actual disk headroom, within the
60 GB policy. Permanent growth reduces the available raw budget. If unique
evidence cannot fit, the worker reports pressure and preserves its last copy;
it must not claim that a finite disk guarantees unlimited permanent history.

On the enrolled research host, every scheduler wake can refresh evidence and
poll public settlements. Midnight Europe/Zurich still freezes exactly one daily
dataset. Label availability is the actual HTTP response receive time; a result
first fetched after midnight cannot enter that midnight's fit. Receipts compare
unique cumulative signal and training IDs, report new labels, and block fits on
unexplained shrinkage. Unchanged training information retains the existing
candidate. No candidate is automatically activated.

The fixed-path SSH reader, when reachable, only permits an rsync sender for
`research/hft_permanent`; it grants no remote shell or upload. Enable the HFT
branch of `research/pull_london_evidence.sh` with `PM_V7_RESEARCH_HFT_ONLY=true`,
the private key outside the repository and the actual London endpoint. Configure
the research source root to the synchronized `compact` directory. A new epoch
uses a new private research root so manifests from an explicitly deleted prior
dataset are never silently mixed into the new history.

Operational outputs are `control/london_buffer_retention_status.json`, its HFT
storage/preservation sections, `research/hft_permanent/compact/population.json`,
the existing per-venue/PM statuses, and `polymarket_v7_hft_*` Prometheus metrics.
Run `monitoring/v7_hft_data_health.py --root RUN_ROOT --seconds 30` for a bounded
sample. Short samples vary with activity; compression estimates are source
specific and historical, not a guarantee of future byte rates.
