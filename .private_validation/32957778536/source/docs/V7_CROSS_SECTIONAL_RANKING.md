# V7 Cross-Sectional Ranking

## Purpose

The cross-sectional ranking sleeve asks a narrower question than a terminal fair-value model:

> conditional on information available at timestamp `t`, which currently tradable Polymarket contracts are likely to outperform or underperform the contemporaneous cross-section over a fixed holding horizon?

The target is therefore a **relative fixed-horizon logit markout**, not a probability of eventual event resolution. This distinction is part of the model contract and is enforced in configuration.

## Horizons

The first research grid is deliberately coarse enough to be economically different from the high-frequency sleeves:

- 30 minutes;
- 1 hour;
- 2 hours;
- 6 hours.

Each horizon has its own labels, training fit, OOS report and admission decision. Horizons are never pooled and the best horizon is not selected on the same observations used to estimate it.

## Target

For YES probability `p_{i,t}`, define `z_{i,t}=logit(p_{i,t})`. The raw future move at horizon `h` is

`Delta z_{i,t+h}=z_{i,t+h}-z_{i,t}`.

At each timestamp the target subtracts the contemporaneous median cross-sectional move. When a sufficiently populated category is available it also uses a shrunk category median. The resulting target is a relative markout residual rather than a market-wide shock.

This makes the sleeve closer to characteristic ranking in finance: it predicts relative future price movement, not the absolute terminal outcome.

## Features

The initial model intentionally uses only information reconstructible from point-in-time price history:

- 1/2/4/12-bucket momentum;
- short and medium distance from trailing mean;
- acceleration;
- short and medium realized logit volatility;
- absolute logit level.

At each timestamp every feature is normalized across the contemporaneous cross-section using median/MAD robust z-scores and clipped. Current liquidity, future activity and present-day book attributes are not inserted into historical feature rows when they cannot be reconstructed causally.

Execution variables are used only after prediction, in the executable selector.

## Estimator

Each horizon uses a recency-weighted ridge regression. Ridge is the first model because the research question is whether a stable cross-sectional linear signal exists at all; a nonlinear learner should not be introduced before this baseline establishes incremental OOS value.

Training is rolling and purged. A row may enter a fit only if its future label is fully known before the fit timestamp minus the configured embargo. The code records both feature timestamp and label timestamp.

## OOS diagnostics

Historical evaluation reports prediction/ranking evidence, not executable PnL:

- Spearman rank IC by cross-section;
- median and mean IC;
- fraction of positive IC periods;
- top-minus-bottom realized logit spread;
- decile target means and monotonicity;
- directional hit rate;
- selected-tail turnover.

`economic_pnl_validated` is always false in the historical evaluator. The sleeve cannot be promoted from price predictability alone.

## Mapping ranks to Polymarket trades

A positive predicted YES-logit move maps to **BUY YES**. A negative predicted YES-logit move maps to **BUY NO**. No synthetic short or target-plus-hedge construction is required.

The executable selector is separate from the predictor and fails closed unless it has:

- a fresh two-sided YES/NO book;
- an authoritative per-market fee descriptor;
- sufficient liquidity;
- acceptable spread;
- positive edge after entry/exit fee estimate, slippage, capital-time charge and adverse-markout penalty.

The economic ranking score is proportional to

`net executable edge / probability uncertainty / sqrt(horizon time)`.

This prevents a six-hour prediction from winning merely because its raw markout is larger while tying up capital for much longer.

## Portfolio constraints

The initial shadow implementation:

- is single-leg only;
- uses TAKER attribution to keep entry semantics observable;
- chooses a bounded number of positive and negative ranks;
- allows at most one selected contract per event in a snapshot;
- allocates a bounded shadow sleeve using economic-score strength;
- never uses binary-outcome Kelly for a fixed-horizon markout target.

The emitted CSV uses the existing V6/V7 broker intent schema so that a future admitted version can use the same broker, risk layer, execution ledger and per-strategy PnL attribution instead of introducing a second execution stack.

## Promotion contract

The research branch is not live-trading code. Promotion requires all of the following on independent evidence:

1. statistical OOS gates pass for at least one horizon;
2. authoritative fee plumbing is shared with the canonical runtime;
3. shadow candidate rows are evaluated through the shared execution ledger;
4. cost-stressed forward PnL is positive and stable enough for governance;
5. no causal leakage, stale book or event-duplication regression is detected;
6. integration occurs through the normal research -> integration -> validation -> paper-validated lifecycle.

Until those conditions hold, `live_intents_enabled=false`, `submitted_orders=0`, and `promotion_ready=false` are invariants.
