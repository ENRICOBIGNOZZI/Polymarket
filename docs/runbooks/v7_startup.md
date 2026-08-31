# V7 startup

Start read-only. Validate exact SHA/config hashes, execution mode, zero caps,
platform-contract snapshot, clock, geoblock/policy and private-state health.
Fetch all session-key orders/trades, wallet activity, positions and chain state;
reconcile and classify or cancel/adopt orphans before any approved mode. A
missing/unknown check means `CANCEL_ONLY` or `KILLED`.
