# Pregame and public-tape safety gates

This note defines the safety behavior introduced after an in-play sports price move was incorrectly surfaced as a statistical-arbitrage residual.

## Sports universe gate

Gamma `sportsMarketType` and `gameStartTime` metadata are parsed at market and embedded-event level.

- Non-sports markets are unaffected.
- A sports market with a missing or invalid game start is rejected.
- A sports market is removed from discovery 60 seconds before the scheduled game start.
- Because B1, B2, the terminal sleeve and the maker simulator all consume the same discovered universe, an in-play sports market cannot enter a fresh signal or order decision.
- A maker order already resting in persisted state carries its sports flag and game start; it is cancelled at the same gate even when the market has disappeared from discovery.

This is intentionally fail-closed. It may reject a valid sports market whose metadata is incomplete, but it cannot reinterpret a live score update as ordinary pregame mean reversion.

## Maker fill evidence

A book touch, a changed last-trade price, or an ask below the paper limit is not fill evidence.

For a passive bid, the simulator now requires a public taker trade that satisfies every condition below:

1. the trade belongs to the same condition and token;
2. its timestamp is strictly later than paper-order creation;
3. the taker side is `SELL`;
4. the trade price is at or below the paper bid, within one quarter tick;
5. the trade has not already been processed by the persisted `(timestamp, trade-key)` cursor.

Eligible trade size first consumes `queue_ahead`. Only excess volume can fill the paper order. Excess volume smaller than the remaining order produces a partial fill; the unfilled remainder stays reserved and resting until another eligible print, cancellation, or TTL expiry.

Public-tape failure, malformed records, pagination limits and late-arriving records can all cause the simulator to miss a fill. They cannot create a fill. This asymmetric behavior is deliberate: paper execution may be pessimistic, but it must not manufacture liquidity.

Legacy V3 resting orders do not contain condition, game-clock or tape-cursor metadata. They are cancelled with `CANCEL_UNVERIFIABLE_TAPE` after upgrade and are never grandfathered into the new fill model.

## Remaining limitations

The implementation polls the public Data API rather than maintaining a lossless exchange WebSocket capture. It therefore does not claim exact historical queue position, cancellation-driven queue improvement, venue latency or atomic multi-leg execution. Those require a continuously persisted event-time recorder and replay tests. Until then, reported maker fills are supported by observed aggressive volume, but should still be treated as conservative paper evidence rather than real execution.

No authenticated order submission or credential handling is included.
