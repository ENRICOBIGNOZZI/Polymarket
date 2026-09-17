# V7 Prepared Order Frame Cache

The external-signal reaction path should not pay EIP-712 hashing, recoverable
secp256k1 signing, JSON serialization, L2 HMAC, or HTTP framing when an identical
aggressive order can be prepared safely before the signal arrives.

`prepared_order::Cache` is a bounded one-producer / one-consumer handoff for a
complete, already-signed HTTP order frame. It owns no signer and no network
socket. The preparation owner publishes an immutable frame with the exact
economic key: instrument, price, tick, quantity, side and time-in-force, plus
its source exchange timestamp and preparation time.

The execution owner acquires the frame only when the current decision matches
that key and the configured freshness bound. Exact source-event matching is the
default. The frame becomes consumed at acquisition, before any network write,
so a transport timeout or partial `SSL_write` can never cause the same salt and
signature to be submitted twice.

Two fixed buffers are alternated. A consumer lease increments the buffer reader
count; the producer cannot recycle that buffer until the execution owner calls
`release()` after the synchronous socket write returns. There is no heap
allocation, mutex, filesystem access, REST call, or signature operation on a
cache hit.

This primitive does not authorize pre-signing by itself. Production wiring must
retain the normal on-demand signing fallback and may use a prepared frame only
when all current risk/depth/market admission gates have already passed. A cache
miss is therefore a latency miss, never an execution-policy relaxation.
