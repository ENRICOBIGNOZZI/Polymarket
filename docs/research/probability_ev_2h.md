# Native probability / execution-value research candidate

## Scope

This change adds an **opt-in PAPER research candidate**, not a proven profitable
model and not an automatic production cutover. Every existing asset remains
registered. No asset exclusion or shadow override is introduced. Existing
single-writer portfolio, capital, risk, OMS, inventory and canonical-ledger
ownership remain authoritative. No authenticated order endpoint is added.

Private ledgers, fitted coefficients, market identifiers and profitability
reports must remain in the private research archive, not this public repository.

## Implemented

- Frozen-ledger audit joins submitted orders to unique causal decision features;
  it labels submitted-but-unfilled orders from public settlement data as well as
  canonical FINAL events. Ambiguous joins and unresolved outcomes remain missing.
- Native expected-value admission compares both actual token asks with a supplied
  settlement-probability interval. DOWN's lower probability is `1-UP_upper`.
  Fees and an explicit, separately identified execution reserve are subtracted.
- Fractional-Kelly sizing uses the conservative net edge, a hard dollar ceiling,
  available canonical capital and visible depth. It never rounds a position UP
  through the risk ceiling to meet the venue minimum. The experimental execution
  limit may chase at most two ticks, and only while the conservative net edge
  remains above the frozen minimum after fee and execution reserve.
- The experimental logistic model uses a market-probability prior and regularized
  deviations for shock size, confirmation, signal age, TTE, spread, imbalance,
  asset and horizon. Markets receive equal training weight. Time-block bootstrap
  covariance is a model-uncertainty proxy, **not a certified conditional LCB**.
- The cold loader checks artifact SHA identity, feature order, covariance,
  finite parameters, PAPER-only flags and risk limits. Inference is native,
  bounded and allocation-free; it does not train or read files on the hot path.
- Order evidence contains forecast, model hash, exact input feature vector,
  the input-token identity, expected net edge, conservative edge and risk size.
  Input-token identity matters when the chosen trade is opposite to the trigger.
- Incremental `v7_nonfill_outcomes.py` persists its cursor and deduplication in
  research-only SQLite. It survives atomic mirror replacements and records
  labels without adding a cent to canonical PnL. Long unresolved markets cannot
  starve later resolution requests.

## Interpretation boundaries

The current fitted model estimates settlement payoff conditional on decision
features in the historical proposal population. It is **not** a validated model
of payoff conditional on obtaining a fill under a different limit, quantity or
latency. The correct action objective requires the joint distribution:

`E[filled_quantity * (settlement_payoff - actual_execution_price) - fees - other_costs | X, action]`.

Multiplying unconditional probability edge by a fill rate generally does not
identify this objective. An execution model must account for selection into
fills, censoring, partial size and actual arrival prices.

The PAPER matcher now carries the actual arrival execution price through the OMS
and capital/inventory cost basis. A FAK fills when the arrival top is at or better
than its limit; visible partial quantity fills and the remainder expires. Price
improvement is therefore not mislabeled as a non-fill. This is still simulated
execution, not exchange-confirmed execution, and unknown venue terms remain a
blocking condition.

Signal selection is context-specific through `config/v7_crypto_signal_policy.json`.
The 30 asset/horizon contexts have independently frozen shock and maximum-age
parameters for the forward PAPER window. These numbers are exploratory rather
than claimed optima. A positive second-venue move is required: a flat venue is no
longer treated as confirmation. BNB keeps Binance spot as the trigger and uses
Bybit spot as an explicitly identified confirmation source because the registered
BNB contexts have no Coinbase spot symbol. Bybit is never relabelled as Coinbase.

When the probability/EV model is explicitly active, its order limit may chase a
small predeclared number of ticks only while the conservative settlement edge
still exceeds fee plus execution reserve. Sizing pays the worst admissible price,
while accounting pays the actual simulated fill price. Optimizing raw fill rate
is not the objective.

Native volatility diagnostics are event-time EWMA quantities, not per-second
settlement volatility. Missing long-horizon history, an opening settlement
reference or oracle basis must not be replaced by numerical zero.

## Asset/context-specific trigger parameters

The same raw 100ms return is not economically equivalent across crypto assets.
The next PAPER cohort therefore freezes a separate `(asset, horizon)` policy for
minimum Binance movement, second-venue confirmation and maximum signal age. The
parameters are intentionally stored outside the hot code path. They are not
post-hoc optimized during the two-hour cohort. Historical diagnostics motivated
the heterogeneity, but prospective results decide whether it survives.

The long-run target is stronger: use a normalized shock such as return divided by
current exchange tick and properly time-normalized volatility, with asset/horizon
calibration on top. Raw bp thresholds are a transitional guard, not the final
alpha representation.

## Two-hour prospective protocol

See `config/v7_probability_forward_2h.json`. The duration is **7200 seconds**, not
8 hours. It begins only when a checked runtime and immutable artifact are
actually activated. Preparing this file does not start a test.

Freeze code SHA, artifact SHA, fee sources, latency semantics, limits and the
analysis plan before the first decision. Record the full six-asset universe and
all configured horizons. No asset-specific post-hoc exclusion is allowed.

Use the decision time to assign orders to the two-hour cohort. The launcher
computes one immutable wall-clock deadline exactly 7,200 seconds after the
private probability artifact is activated and passes that same deadline to all
30 contexts. After the deadline the probability lane fails closed to new taker
risk while feeds, risk, ledger and settlement remain active. Markets that settle
after the two-hour boundary are pending, not losses, wins or zeros. Two hours
is an operational research window, not a guarantee of statistical precision.

Primary diagnostics are calibration/log loss against the market prior,
fill-conditioned net PnL, quote/arrival degradation, actual cost and uncertainty
coverage by market and time block. Nonfill limit-price payoffs must be labelled
hypothetical and not aggregated over mutually exclusive retries as a strategy.
No automatic promotion or enlargement of risk follows a positive sample.

## Cold-plane usage

1. Run the audit against a frozen ledger and the corresponding observation files.
2. Fit on the research machine with `v7_fit_probability_candidate.py`. Keep the
   artifact outside Git. Its exact `code_sha` must match the staged runtime.
3. Validate the native binary with `--probability-model /private/model.json
   --model-sha <exact-sha> --validate-only`.
4. For an explicitly authorized, checked activation, set
   PM_V7_PROBABILITY_MODEL=/private/model.json. The launcher checks that the
   file exists and passes it to the manager. Without that environment variable,
   the probability model remains disabled.
5. Keep the rollout gated until the empirical candidate, source coverage, joint
   execution interpretation and normal exact-SHA deployment checks are reviewed.

## Remaining work before claiming a completed redesign

- Replace transitional raw-bp shock gates with causal tick- and time-volatility-
  normalized features, then re-estimate context parameters prospectively.
- Actual settlement opening reference, oracle basis, per-time volatility and
  expiry semantics in the feature contract.
- Joint fill/payoff modelling and out-of-sample checks for action/size changes.
- Fit chase/fill/slippage from the new bounded post-decision book windows rather
  than selecting execution parameters from settlement PnL alone.
- Prospective two-hour activation and resulting forward evidence.

### Integration activation boundary

In the unified multi-rate launcher both `PM_V7_PROBABILITY_MODEL` and
`PM_V7_CRYPTO_SIGNAL_POLICY` are empty by default. The new context thresholds
and TTLs are a separately activated research policy, not a demonstrated
improvement or an automatic consequence of deploying an infrastructure patch.
Native source: `src/v7_crypto_settlement_engine.cpp`.
