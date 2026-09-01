# Crypto settlement engine

V7 treats settlement alpha as one economic decision, not competing strategies.
`crypto_settlement_fair`, `professional_maker`, and `crypto_informed_taker` are
components of `CRYPTO_SETTLEMENT_ENGINE(asset, horizon)`. None is an authority owner. Structural
arbitrage is the second engine; `hard_arb` and `fast_structural` are its
components. Both engines publish typed opportunities to the same global
coordinator and share the canonical allocator, risk, OMS, inventory, and ledger.

## Decision contract

For each causal cut the bounded C++ kernel evaluates safety actions first. If
no critical cancel, withdraw, liquidation or inventory-reduction action is
required, `MAKE` and `TAKE` compete on conservative expected account wealth.
Neither action receives priority merely because it is maker or taker.

Maker value is computed as:

```text
P_lower(reach)
* P_lower(fill | reach)
* (robust capture + rebate_lower
   - adverse_markout_upper - p99_cancel_latency_risk)
- inventory_cost - cancel_cost - capital_cost
```

Reach, conditional fill and fill-conditioned markout must all come from a
mature exact-SHA execution artifact. Rebate is credited only inside the fill
term. The taker path likewise requires a mature fill/race model and charges
the empirical p99 arrival latency in addition to observed book age.

## Asset and horizon isolation

The contract, fair model and execution policy must carry the same immutable
horizon identity:

- BTC 5m: maker from 300–120 seconds TTE; taker from 120–5 seconds.
- BTC 15m: maker from 900–30 seconds; late taker from 120–5 seconds.
- ETH, SOL, and XRP 5m/15m: verified market/settlement mappings, but
  `SHADOW_ZERO_AUTHORITY` until independent models and economic evidence mature.
- 1m, 1h, and 4h remain representable software horizons but are not registered
  contexts and therefore fail closed.

A model cannot cross an asset, horizon, or settlement-semantic hash. The default C++ bridge
coefficient is zero and the model is invalid until the slow plane declares its
parameters empirically fitted. The 60-second Chainlink TWAP remains the
settlement source; the 30-second TWAP and derivatives are predictors only.

The canonical registry is
[`v7_crypto_settlement_markets.json`](../../config/v7_crypto_settlement_markets.json).
It contains eight independently observed market contexts (BTC/ETH/SOL/XRP ×
5m/15m), their exact Chainlink 60-second-TWAP rules, source symbols, and the
live slug used for verification. The separate immutable model registry is
indexed by asset, horizon, and settlement-semantic hash. It currently contains
only unregistered shadow slots, so no context gains new-risk authority merely
from being present in the market registry. Non-BTC data sources are likewise
registered but not launched until their causal collection path is validated.
Discovery can only transition a verified market to research/shadow collection;
it can never execute or promote it automatically. All assets compete for the
same global capital pool, and crypto-directional exposure receives no fake
per-asset diversification credit.

## Evidence cold start

[`v7_crypto_settlement_engine_contract.py`](../../scripts/v7_crypto_settlement_engine_contract.py)
cross-checks the engine policy, strategy registry and live scope, then binds:

- exact code SHA;
- p50/p90/p95/p99/p99.9/max for taker arrival, maker place ACK, maker cancel
  ACK and private-WebSocket confirmation;
- maker reach, conditional-fill and fill-conditioned-markout evidence;
- one horizon-specific policy.

Configured latency constants and public GET timings are not observations. If
any required empirical segment is missing, the frozen runtime snapshot keeps
new risk disabled while leaving critical cancel/withdraw independent.

The canonical launch loop always freezes this snapshot. During the current
cold start it intentionally produces `CANCEL/WITHDRAW/NOTHING` authority only. Passing
future evidence changes the snapshot, not the safety or ownership model, and
does not automatically promote execution.
