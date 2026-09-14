# V7 Historical PnL Regime Matrix — 2026-09-14

This note records only evidence observed before the active `aa5311a...` forward window. It is development evidence, not a promotion decision. The current forward window must not be inspected or changed because of these results.

## System-level diagnosis

Historical generalized Maker PAPER runs: 4,752 submitted orders, 101 fills (about 2.13%), 97 terminal units, 24 positive finals, 73 non-positive finals, aggregate realized PnL about -55.72. This rejects order-count and quote-everywhere as objectives.

The frozen external-cancel forward report is materially stronger evidence: 4,216 episodes across 116 markets, 499 avoidable fill events / 2,018.30 shares, +0.04026/share equal-weight 500 ms avoided-markout improvement, +0.03792/share after leaving the best market out, and 97.17% positive markets. Under 3x queue plus 200 ms cancel stress the improvement is +0.04325/share. The rule passed its registered forward gate.

Short-horizon repricing also contains information. On the frozen 95-market repricing forward set, PM+external improved 250 ms MSE by about 18.64% versus zero-change and 11.87% versus PM-micro alone. This is predictive evidence, not by itself trading PnL.

Across seven completed confirmatory windows with 669 contract-window observations, however, the 1 s settlement surplus under 2x cost/risk stress averaged about -0.01398/share. Generic directional trading therefore remains rejected.

## Latency decay — latest two comparable windows only

Using contract-equal weighting across the latest two same-generation windows, settlement surplus under 2x cost/risk stress changed with observation delay approximately as follows:

| Delay | Mean surplus/share |
| ---: | ---: |
| 0 ms | +0.00358 |
| 100 ms | +0.00221 |
| 250 ms | -0.00124 |
| 500 ms | -0.00160 |
| 1000 ms | -0.00270 |

This motivates a prospective 250 ms entry ceiling and aggressive removal of artificial entry delay. It does not justify bypassing fresh-arrival revalidation.

## Predeclared margin bins — latest two comparable windows only

At 1 s and 2x cost/risk stress, the registered margin bins had approximate contract-equal mean settlement surplus/share:

| Margin bin | Mean surplus/share |
| --- | ---: |
| lowest | -0.00536 |
| second | +0.00703 |
| third | -0.01228 |
| highest | +0.04224 |

The non-monotonic middle bins are a warning against fitting a threshold from this sample. The challenger therefore freezes +0.01/share after 2x costs as its primary threshold and keeps 0.005/0.03/0.10 as shadow research arms.

## TTE and side — descriptive only

At 1 s and 2x cost/risk stress, the latest two windows showed approximately -0.0232/share for the shortest TTE bin, -0.01235 for the middle bin and +0.00616 for the longest registered TTE bin. YES-side selected origins were near flat (-0.00048/share), while NO-side selected origins were materially negative (-0.06509/share).

These are descriptive regime diagnostics. Side or TTE must not become a live filter from the same sample; they remain frozen reporting dimensions in the next forward study.

## Capacity — latest two comparable windows only

For first-per-contract candidates whose development margin was at least 1 cent, the 1 s arrival book had median L1 ask depth around 257 shares and roughly 10th-percentile depth around 37 shares. Of 191 contracts, 191 had at least 5 shares visible, 190 at least 10, 178 at least 25, 163 at least 50 and 145 at least 100.

The historical mean deteriorated when naively scaling to 100 shares, so the evidence does not justify large sizing. Capacity must be treated as a joint function of depth, edge and regime rather than a fixed maximum.

## Prospective interpretation

1. Maker must optimize conservative fill-adjusted EV, not quote count.
2. External information has its strongest demonstrated use as a Maker veto / fast cancel.
3. Directional execution is a separate selective lane, not a fallback that trades every forecast.
4. Entry latency matters economically, especially beyond 100–250 ms, but latency work cannot rescue a negative selection policy by itself.
5. The next forward report must publish TTE, side, spread, book imbalance, external disagreement, 100 ms external shock, volatility and oracle-distance cells without changing the primary threshold after the window starts.
