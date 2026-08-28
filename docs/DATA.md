# V7 Data

Receive time is the actionable clock. Raw events preserve source event time, exchange time, local monotonic receive time, sequence, connection epoch, gap/stale flags and provenance.

The canonical tape covers Polymarket L2/trades, settlement oracle/reference, external venues, structural events, OSINT, sports, wallet flow, decisions, orders, fills, cancels, inventory, risk and fair snapshots.

Hot callbacks write compact events to bounded queues; asynchronous recorders perform persistence. The decision path never blocks on filesystem or database I/O.

Evidence is exact-SHA and uses the correct independent statistical unit: contract, order clustered by event/session, arb bundle, independent information event, game sequence, wallet-event cluster or market/event/time cluster.

Dataset, build and run manifests bind source inputs, mappings, universe,
configuration, models, binaries and SBOM hashes. OSINT, market-open, sports,
cross-platform and wallet datasets use append-only causal tapes with explicit
gap, correction, provenance and quarantine state.

The EVENT-2 OSINT slow plane reads `config/v7_osint_sources.json` and writes
`runs/paper_v7_live/osint/raw_events.jsonl`. Each record binds the stable source
event identity to published time, local receive time, payload hash, root
lineage, correction predecessor, transport and connection epoch. Corrections
append a new record; they never overwrite history. Conditional HTTP state lives
outside the tape and is used only for ETag/Last-Modified retrieval and exact
duplicate suppression.

The FAST-1 Market Open collector records only listings first observed after its
initial baseline snapshot. It emits deterministic one-time milestones for
creation, active book, first quote, first depth and first observed trade. Raw
rules are hashed, but semantic verification remains `UNVERIFIED` until an
approved parser/mapping certifies the exact settlement contract.
