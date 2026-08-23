# In-play and maker-fill safety hotfix

This hotfix removes two sources of false paper alpha.

## Timed sports gate

Gamma `gameStartTime`, `sportsMarketType`, and `secondsDelay` metadata are stored on each discovered market. B1, B2, and maker entry reject a timed sports market when:

- its game-start timestamp is missing or invalid; or
- the current time is inside the fixed 15-minute pre-game buffer; or
- the game has already started.

The gate is strategy-scoped: structural arbitrage and genuine terminal-probability scans retain their universe, while only mean-reversion and maker-entry sleeves are blocked around game start.

Outstanding simulated maker orders carry their persisted game-start timestamp and are cancelled with `CANCEL_GAME_START` at the start time. Any partially filled position is then unwound as a taker when displayed depth permits.

## Tape-driven maker fills

A book touch, a changed last-trade price, or an ask below the order limit is no longer sufficient fill evidence.

The paper maker now polls public taker trades from the Polymarket Data API and admits only post-order aggressive `SELL` trades in the same outcome token at or below the passive bid limit. Eligible size is applied in this order:

1. consume persisted `queue_ahead`;
2. partially fill the resting order with any excess aggressive size;
3. keep the residual order resting until later eligible trades, cancellation, or TTL.

Trade cursors and same-timestamp trade IDs persist across restarts. If the trade tape is missing or unavailable, the simulator records no fill.

## State migration

Legacy V3 resting orders are not restored because they do not contain a condition ID, game clock, or trade-tape cursor. Existing V3 positions remain loadable and retain conservative taker exit behavior.

## Remaining limitations

This is still a paper-only polling simulator. It does not reconstruct exact exchange queue priority, hidden liquidity, self-trades, websocket packet ordering, submission latency, or atomic multi-leg execution. A simulated fill is therefore evidence under the documented queue model, not proof that a live order would have filled.
