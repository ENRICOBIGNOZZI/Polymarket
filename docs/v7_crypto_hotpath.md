# V7 crypto hot path

This change narrows the latency work to crypto external-information trading. It does not extend or optimize Structural Arb.

## Fast path

The opt-in path is:

`external venue frame -> external state -> frozen signal -> local datagram -> taker -> mmap Polymarket L10 -> Unix coordinator request/reply -> PAPER lifecycle spool`

The path is enabled only when `PM_V7_CRYPTO_HOTPATH_IPC=1`. The default launcher semantics remain unchanged.

The frozen BTC M5 signal still uses its existing 25ms evaluation grid, 100ms shock window and 250ms cooldown. Transport improvements do not silently redefine the experiment.

Polymarket executable books are maintained by the existing canonical WebSocket decoder. A fixed-size local mmap exports the current L10 snapshot. The taker rejects stale, invalid, non-continuous, wrong-SHA or torn snapshots.

`min_order_size` is captured by the canonical C++ cold-start/recovery book bootstrap and exported in the same mmap slot. After bootstrap, the taker performs no `/books` REST call on either the trigger path or a metadata side path. If the mmap snapshot is stale or invalid, it fails closed.

The coordinator can receive a candidate through a bounded Unix stream and reply directly. The accept thread performs framing only; economic logic remains on the single coordinator owner thread.
## Slow path

Settlement HTTP, metadata refresh, status/audit persistence, research tapes and analytics are never prerequisites for reacting to a fresh signal. Settlement requests run on a dedicated worker and results are reconciled by the owner thread.

Signal JSON persistence remains for recovery/evidence. In hot mode, the external runtime sends the same signal object over a local Unix datagram before asynchronous persistence. A file watcher remains as a recovery fallback.

## Binance SBE

The diagnostic SBE path implements Binance Spot `stream_1_0`, schema id 1/version 0. It supports `BestBidAskStreamEvent` and `TradesStreamEvent` parsing with bounded message and group sizes.

The JSON/SBE race probe compares the same `bookUpdateId` and identical BBO values on the same London host. SBE requires an Ed25519 API key in `X-MBX-APIKEY`; no SBE performance claim is made before that race is run.

SBE `bestBidAsk` uses auto-culling under load. A lower arrival time therefore does not imply equivalent event history. Any feature migration must separately test semantic parity and predictive impact.

## Coinbase

The production source remains Exchange `level2_batch` until a faster path wins a same-host causal race. Coinbase documents Exchange `level2` as unbatched, but the current unauthenticated Exchange endpoint returned `Failed to subscribe` in the live probe. Advanced Trade public `level2` connects without credentials after allowing its >4 MiB BTC-USD bootstrap snapshot, but in the first clean Mac race its matched unique updates arrived materially later than Exchange `level2_batch`. These are empirical observations, not permanent venue guarantees; repeat in London before any feed change.

The test must compare receive-time freshness, update count, reconstructed BBO parity, gaps/reconnects and downstream frozen-signal differences. A faster feed is not promoted merely from message-count or ping measurements.
## Evidence boundaries

Synthetic Unix IPC, mmap and queue measurements are internal host benchmarks only. They do not prove exchange network latency, order acknowledgment latency, fill probability, profit or world-best performance.

A London integration gate requires: exact candidate SHA, clean Release/Debug tests, real public Polymarket WebSocket smoke, zero hot-cache lineage errors, zero IPC drops, and a separately identified PAPER cohort.

No authenticated order endpoint is used by the validation probes. Real order submission remains false.

## Cutover rule

The old PAPER cohort is never modified in place. Warm the candidate first, prove its feed/cache state, stop new proposals on the prior owner, reconcile positions/reservations, then transfer the single owner. Rollback uses current reconciled state rather than an old snapshot.

Credentials are never committed. Coinbase Exchange credentials and the Binance SBE API key are supplied only through the runtime secret environment on the selected London host.
