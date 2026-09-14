# V7 Historical PnL Regime Matrix — 2026-09-14

This note records only evidence observed before the active `aa5311a...` forward window. It is development evidence, not a promotion decision. The current forward window must not be inspected or changed because of these results. Every regime statement below is therefore a hypothesis generator unless explicitly tied to an independently registered forward gate.

## System-level diagnosis

Historical generalized Maker PAPER runs: 4,752 submitted orders, 101 fills (about 2.13%), 97 terminal units, 24 positive finals, 73 non-positive finals, aggregate realized PnL about -55.72. This rejects order-count and quote-everywhere as objectives.

The frozen external-cancel forward report is materially stronger evidence: 4,216 episodes across 116 markets, 499 avoidable fill events / 2,018.30 shares, +0.04026/share equal-weight 500 ms avoided-markout improvement, +0.03792/share after leaving the best market out, and 97.17% positive markets. Under 3x queue plus 200 ms cancel stress the improvement is +0.04325/share. The rule passed its registered forward gate.

Short-horizon repricing also contains information. On the frozen 95-market repricing forward set, PM+external improved 250 ms MSE by about 18.64% versus zero-change and 11.87% versus PM-micro alone. This is predictive evidence, not by itself trading PnL.

Across seven completed confirmatory windows with 669 contract-window observations, however, the 1 s settlement surplus under 2x cost/risk stress averaged about -0.01398/share. Generic directional trading therefore remains rejected.

## Cross-window stability — same frozen model only

To avoid mixing model generations, a stricter check uses the five contiguous completed windows sharing frozen model hash `ac837540...`. Their contract-equal 1 s settlement surplus under 2x cost/risk stress was approximately:

| Window | Mean surplus/share |
| ---: | ---: |
| 1 | -0.03949 |
| 2 | -0.03894 |
| 3 | +0.00221 |
| 4 | -0.02114 |
| 5 | +0.01555 |

No predeclared regime cell with at least 10 independent contracts in a window was positive in four or more of these five windows. This is the strongest current warning against selecting a profitable-looking cell from one historical slice.

The side diagnostic is asymmetric but remains descriptive. NO-side contract-equal mean surplus/share was negative in all five windows: about -0.0455, -0.0352, -0.0072, -0.0630 and -0.0672. YES was much less stable. A post-hoc first-per-contract YES subset above 1 cent development margin was positive in four of five windows, while the analogous NO subset remained negative in all five; neither observation is permitted to become a live side filter from this sample.

## Maker fill-conditioned toxicity

Across archived pre-window Maker markout evidence there were 109 distinct fills with a 1 s markout and 104 with a 10 s markout. Mean fill-conditioned markout was approximately -0.0422/share at 1 s and -0.0632/share at 10 s. Only about 11.9% of 1 s markouts and 23.1% of 10 s markouts were positive.

This is direct evidence that generalized passive fills are adversely selected. It supports external cancel/veto and selective quoting more strongly than it supports simply increasing Maker fill rate.

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
6. The NO-side asymmetry is a prospective hypothesis only. It requires a fresh boundary before it can affect selection or sizing.
