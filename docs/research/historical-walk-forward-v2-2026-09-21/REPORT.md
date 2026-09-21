# HISTORICAL WALK-FORWARD V2

## Direct answers

- 500ms-reference OOS executable markout: **POSITIVE POINT ESTIMATE / UNCERTAIN**
- Settlement alpha: **UNKNOWN / INSUFFICIENT CAUSAL SETTLEMENT LABELS**
- PM lag after causal external signal: **POSITIVE DESCRIPTIVE 250MS MOVE**
- Prior zero-trade diagnosis: **PM midpoint baseline cannot cross executable ask plus costs.**
- Principal bottleneck: **SEE_FUNNEL_AND_FRICTION_DECOMPOSITION**

## Identity

- Starting SHA: 53ed4b3387fe18332a742b99d800f9b2ea0f28a9
- Data SHA: a6e44d7665d2aac9d7edb8f8ad0e3c0eee1d13c04a0ef8f9653895a1627e6a77
- Input state: READY
- Decisions: 330677
- Markets: 1714
- Short-horizon pairs: 65950
- OOS predictions: 239957
- Full-window midpoint models ready: 7
- Full-window executable-markout models ready: 7

## 500ms reference executable-markout economics

- Simulated orders: 17
- Fills: 4
- Marked fills: 4
- Positive marked fills: 2
- Net executable markout: 0.17329999999999973
- Markout/fill: 0.04332499999999993
- Fill rate: 0.23529411764705882

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
