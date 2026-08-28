# V7 Data

Receive time is the actionable clock. Raw events preserve source event time, exchange time, local monotonic receive time, sequence, connection epoch, gap/stale flags and provenance.

The canonical tape covers Polymarket L2/trades, settlement oracle/reference, external venues, structural events, OSINT, sports, wallet flow, decisions, orders, fills, cancels, inventory, risk and fair snapshots.

Hot callbacks write compact events to bounded queues; asynchronous recorders perform persistence. The decision path never blocks on filesystem or database I/O.

Evidence is exact-SHA and uses the correct independent statistical unit: contract, order clustered by event/session, arb bundle, independent information event, game sequence, wallet-event cluster or market/event/time cluster.

Dataset, build and run manifests bind source inputs, mappings, universe,
configuration, models, binaries and SBOM hashes. OSINT, market-open, sports,
cross-platform and wallet datasets use append-only causal tapes with explicit
gap, correction, provenance and quarantine state.
