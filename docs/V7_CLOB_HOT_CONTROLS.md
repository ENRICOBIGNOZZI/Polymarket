# V7 CLOB hot controls

This candidate closes three transport/control gaps without granting execution authority.

## Warm order and cancel lanes

`HotConnectionLanes` owns two independent persistent certificate-verified TLS sessions to the same CLOB host. DNS, TCP and TLS setup occur during prewarm. Placement traffic cannot create client-side head-of-line blocking for a risk-driven cancel. If either lane cannot prewarm, both are closed and the component fails closed.

## Local rate limiting

`ClobRateLimiter` keeps order and cancel token buckets independent, matching Polymarket's per-signer separation. Defaults keep 10% headroom below the current Standard signer limits: 36 order tokens/s with burst 54, and 72 cancel tokens/s with burst 108. A second local Cloudflare guard runs below the documented sustained single-order endpoint limit. Policies are cold-start configurable for higher account tiers.

The limiter never sleeps in the hot path. It either admits immediately or returns the earliest local monotonic timestamp at which the request can be admitted. Response-header remaining-token evidence may only clamp the local view downward.

## User WebSocket

`UserWebSocketFeed` connects only to `wss://ws-subscriptions-clob.polymarket.com/ws/user`, authenticates in the initial subscription message, uses certificate/hostname verification and `TCP_NODELAY`, sends the documented text `PING` every 10 seconds, and reconnects with bounded backoff.

`UserWsParser` converts order and trade messages to fixed POD records carrying local monotonic receive time. Trade messages emit correlation records for the taker order and each maker order. A later exact order-ID map discards foreign IDs and deduplicates `(trade_id, order_id)` before OMS fill application.

After a user-stream reconnect, REST remains a cold reconciliation boundary only. It is not part of the reaction path.

No private key, order signing, order submission, sizing, risk, strategy, ledger ownership or real-money authority is added by this candidate.
