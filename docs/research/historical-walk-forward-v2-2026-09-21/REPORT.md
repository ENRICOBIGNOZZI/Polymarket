# HISTORICAL WALK-FORWARD V2

## Direct answers

- 500ms-reference OOS executable markout: **POSITIVE POINT ESTIMATE / UNCERTAIN**
- Settlement alpha: **UNKNOWN / INSUFFICIENT CAUSAL SETTLEMENT LABELS**
- PM lag after causal external signal: **POSITIVE DESCRIPTIVE 250MS MOVE**
- Prior zero-trade diagnosis: **PM midpoint baseline cannot cross executable ask plus costs.**
- Principal bottleneck: **SEE_FUNNEL_AND_FRICTION_DECOMPOSITION**

## Identity

- Starting SHA: 12a8b17ddcaa3eabad50bdac2d22ed27797a51f2
- Data SHA: f5b28084101709f8fc3d6dcf7172153540cd9bbb5e022caaa0b555fefb288ff1
- Input state: READY
- Decisions: 398781
- Markets: 1952
- Short-horizon pairs: 77596
- OOS predictions: 298392
- Full-window midpoint models ready: 7
- Full-window executable-markout models ready: 7

## 500ms reference executable-markout economics

- Simulated orders: 52
- Fills: 23
- Marked fills: 18
- Positive marked fills: 5
- Net executable markout: 0.12904899999999916
- Markout/fill: 0.007169388888888842
- Fill rate: 0.4423076923076923

The report never converts unavailable books, labels, forecasts, or fills into zero.

![01-walk-forward-cumulative-pnl.png](01-walk-forward-cumulative-pnl.png)

![02-pnl-by-model.png](02-pnl-by-model.png)

![03-pnl-by-latency.png](03-pnl-by-latency.png)

![04-edge-decay-vs-latency.png](04-edge-decay-vs-latency.png)

![05-predicted-edge-vs-markout.png](05-predicted-edge-vs-markout.png)

![06-predicted-edge-vs-settlement-pnl.png](06-predicted-edge-vs-settlement-pnl.png)

![07-funnel-survival.png](07-funnel-survival.png)

![08-pm-baseline-executable-edge.png](08-pm-baseline-executable-edge.png)

![09-model-logloss-comparison.png](09-model-logloss-comparison.png)

![10-repricing-prediction-quality.png](10-repricing-prediction-quality.png)

![11-pnl-by-asset.png](11-pnl-by-asset.png)

![12-pnl-by-contract-horizon.png](12-pnl-by-contract-horizon.png)

![13-friction-decomposition.png](13-friction-decomposition.png)

![14-fill-rate-vs-predicted-edge.png](14-fill-rate-vs-predicted-edge.png)

![15-equity-drawdown-500-1000-2000ms.png](15-equity-drawdown-500-1000-2000ms.png)
