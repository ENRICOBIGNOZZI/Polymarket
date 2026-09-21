# HISTORICAL WALK-FORWARD V2

## Direct answers

- 500ms-reference OOS executable markout: **NEGATIVE POINT ESTIMATE / UNCERTAIN**
- Settlement alpha: **UNKNOWN / INSUFFICIENT CAUSAL SETTLEMENT LABELS**
- PM lag after causal external signal: **POSITIVE DESCRIPTIVE 250MS MOVE**
- Prior zero-trade diagnosis: **PM midpoint baseline cannot cross executable ask plus costs.**
- Principal bottleneck: **SEE_FUNNEL_AND_FRICTION_DECOMPOSITION**

## Identity

- Starting SHA: ffb951a7568f414e26a9c9b7effeb9dce3298d5e
- Data SHA: 71ec205434a5d08155c206f2961fba60a12b0c615f105b3727818fc33f468d1a
- Input state: READY
- Decisions: 408873
- Markets: 2000
- Short-horizon pairs: 79246
- OOS predictions: 306672
- Full-window midpoint models ready: 7
- Full-window executable-markout models ready: 7

## 500ms reference executable-markout economics

- Simulated orders: 55
- Fills: 28
- Marked fills: 22
- Positive marked fills: 5
- Net executable markout: -0.3006510000000015
- Markout/fill: -0.013665954545454613
- Fill rate: 0.509090909090909

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
