# Canonical public-book observations

The existing native maker fillability observer also records receive-time book
states and canonical maker features. It remains a public, PAPER-only observer
with no execution authority. The current fair-market YES/NO pair is included
for research even when it is not maker-eligible. Market rollover recreates the
same observer and establishes a new session; it does not authorize an order.

`micro_maker/book_observations/current.jsonl` contains every decoded observation.
The producer seals segments at 64 MiB; retention compresses only closed files,
verifies decompressed byte identity, and preserves the active current file.
Session, connection epoch, sequence, exact code SHA and both receive clocks
identify each record. Reconnects, malformed frames, decoder failures and queue
drops invalidate continuity. The JSON contains L1 prices/depth and derived
features; it is not a complete depth-by-price exchange replay.

Feature estimates reuse `MakerInstrumentLane`, with a one-second warmup after
initialization, reconnect or tick-regime change. Cancellation intensity is the
existing L5 contraction minus observed trades proxy, not an exchange cancellation
count. Inventory is missing in market data. Admission identifies zero inventory
only from a fresh complete flat-account proof with no local pending orders;
non-flat inventory remains missing and is excluded from complete-vector training.
Native admission preserves the original selection timestamp, records the feature
snapshot separately, validates freshness/session/tick identity, and measures
distance from the actual observed touch. New evidence cannot refresh old authority.

The lead/lag collector uses `CAUSAL_BOOK_STATE_AT_HORIZON`: the last valid YES/NO
states received at or before origin and origin + 100/250/500/1000 ms. A continuous
processed-event watermark must cover the target; a status heartbeat alone cannot
complete a label. Receive wall clocks have millisecond resolution. A quiet book
can remain unchanged if stream continuity is proved. Missing coverage, complement
inconsistency and gaps are explicitly censored after a two-second grace period.
The original fair/router prior is retained separately from the actual book prior.

Training must explicitly select this target with
`--target-semantics CAUSAL_BOOK_STATE_AT_HORIZON`; the default remains the legacy
router snapshot target. Mixed target semantics are rejected. Inference with a
book-target model requires a matching causal book prior. No model is automatically
trained or promoted by this collection change, and new coverage does not establish
predictive value or profitable execution. The full-vector native integration and
causal-history tests establish the data path; forward market evidence must establish
model quality separately.
