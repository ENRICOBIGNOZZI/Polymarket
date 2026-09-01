# BTC settlement engine

V7 now treats settlement alpha as one economic decision, not three competing
strategy sleeves. `crypto_settlement_fair` is the registry authority owner;
`professional_maker` and `crypto_informed_taker` are execution-model
components. They cannot own capital or submit a second independent decision.
Structural arbitrage remains separate under `hard_arb`, with
`fast_structural` retained as a policy challenger.

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

## Horizon isolation

The contract, fair model and execution policy must carry the same immutable
horizon identity:

- BTC 5m: maker from 300–120 seconds TTE; taker from 120–5 seconds.
- BTC 15m: maker from 900–30 seconds; late taker from 120–5 seconds.
- BTC 4h: research only, with no new-risk action.

A 5m model cannot price a 15m or 4h contract. The default C++ bridge
coefficient is zero and the model is invalid until the slow plane declares its
parameters empirically fitted. The 60-second Chainlink TWAP remains the
settlement source; the 30-second TWAP and derivatives are predictors only.

## Evidence cold start

[`v7_btc_settlement_engine_contract.py`](../../scripts/v7_btc_settlement_engine_contract.py)
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
cold start it intentionally produces `CANCEL/NOTHING` authority only. Passing
future evidence changes the snapshot, not the safety or ownership model, and
does not automatically promote execution.
