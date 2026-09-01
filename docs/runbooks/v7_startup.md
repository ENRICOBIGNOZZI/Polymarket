# V7 startup

Start read-only. Validate exact SHA/config hashes, execution mode, zero caps,
platform-contract snapshot, clock, geoblock/policy and private-state health.
Create a redacted immutable session registry, then fetch all session-key
orders/trades, wallet activity, positions and chain state. Reconcile and
classify or cancel/adopt orphans before any mode change. A
missing/unknown check means `CANCEL_ONLY` or `KILLED`.
