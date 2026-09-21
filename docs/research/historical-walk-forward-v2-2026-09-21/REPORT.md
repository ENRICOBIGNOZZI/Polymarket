# HISTORICAL WALK-FORWARD V2

## Direct answers

- 500ms-reference OOS executable markout: **POSITIVE POINT ESTIMATE / UNCERTAIN**
- Settlement alpha: **UNKNOWN / INSUFFICIENT CAUSAL SETTLEMENT LABELS**
- PM lag after causal external signal: **POSITIVE DESCRIPTIVE 250MS MOVE**
- Prior zero-trade diagnosis: **PM midpoint baseline cannot cross executable ask plus costs.**
- Principal bottleneck: **SEE_FUNNEL_AND_FRICTION_DECOMPOSITION**

## Identity

- Starting SHA: 8aade0b5cf335ed75883e35d5f42f17898c0112f
- Data SHA: 1914fe94d688dc5787adf1660cf2a15f669204b6cc4f0eacb13eee65ee2f432b
- Input state: READY
- Decisions: 383641
- Markets: 1856
- Short-horizon pairs: 74839
- OOS predictions: 289064
- Full-window midpoint models ready: 7
- Full-window executable-markout models ready: 7

## 500ms reference executable-markout economics

- Simulated orders: 39
- Fills: 20
- Marked fills: 13
- Positive marked fills: 5
- Net executable markout: 0.12879899999999875
- Markout/fill: 0.009907615384615289
- Fill rate: 0.5128205128205128

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
