# LEAD_LAG_TAKER_V1 capacity replay

This is a read-only PAPER capacity analysis. It cannot submit orders, change
risk, or promote a strategy. The statistical unit is an independent BTC M5
market. Historical capacity uses the exact arrival best ask and visible size
recorded in the canonical ledger.

## Evidence snapshot

The frozen forward run produced 46 candidate attempts across 38 unique markets.
Thirty-one markets eventually filled and settled; 21 won and 10 lost. Original
5-share realized PnL was +$24.028725. Seven candidate markets never filled, so
capacity participation is reported against all 38 candidate markets, not only
the 31 successful 5-share fills.

The best-ask visible-size distribution across the 31 filled markets had median
80 shares, mean 290.28, minimum 5, maximum 1,506.06. Same-price notional
capacity had median $26.59, mean $136.35, minimum $1.05 and maximum $807.13.

## Frozen no-chase fixed-share capacity

| Target shares | Full same-price fills | Participation / 38 candidates | Peak historical entry cash |
| ---: | ---: | ---: | ---: |
| 5 | 31 | 81.6% | $8.87 |
| 25 | 21 | 55.3% | $39.80 |
| 50 | 16 | 42.1% | $50.12 |
| 100 | 15 | 39.5% | $97.20 |
| 250 | 12 | 31.6% | $243.01 |
| 500 | 10 | 26.3% | $486.02 |
| 1,000 | 1 | 2.6% | $365.93 |

These are exact historical counterfactuals only while the full requested size
fits at the observed best ask. They do not sweep to worse prices. Larger sizes
therefore trade less often rather than inventing slippage.

## Fixed-dollar capacity

A fixed dollar budget is more meaningful than shares because contract prices
range from near zero to near one. Under the same no-chase rule, $5 per entry
would have fit 25/38 candidate markets (65.8%); $25 fit 16/38 (42.1%); $50 fit
12/38 (31.6%); $100 fit 9/38 (23.7%); $250 fit 7/38 (18.4%); $500 fit 4/38
(10.5%); and $1,000 fit none.

For same-price capacity among the 31 markets that actually filled at 5 shares,
the largest fixed notional supported by at least 90%/75%/50%/25% of those
arrival snapshots was respectively about $2.90 / $5.40 / $26.59 / $148.80.

Displayed liquidity can cancel before a real order arrives. The report therefore
also recomputes every grid assuming only 50%, 25%, or 10% of displayed best-ask
size survives. This is a sensitivity analysis, not an estimated cancellation
model.

## Boundary and next evidence

The historical ledger retained total ask depth but not the price/size ladder for
every level. Total depth alone cannot determine VWAP, slippage, fees by price,
or PnL for a sweep. The replay therefore refuses to infer deeper-book prices.

This branch adds the complete arrival ask ladder and authoritative fee schedule
to future `ORDER_SUBMITTED` PAPER evidence under `metadata.capacity_book`.
Once new forward observations exist, multi-level sweep capacity can be measured
rather than guessed. Until then, the defensible historical capacity result is
top-of-book/no-chase capacity plus displayed-depth survival stress.

Runtime output is written under
`runs/paper_v7_live/research/lead_lag_capacity_v1/`: `report.json`,
`scenarios.csv`, and `trades.csv`. The analyzer is
`scripts/v7_lead_lag_capacity_replay.py`.
