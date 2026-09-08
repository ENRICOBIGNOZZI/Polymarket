# V7 models

V7 separates prediction from execution.

## Settlement / fair-value model

The BTC M5 fair-value model is an offline research artifact. Training is explicit on receive-time-causal, whole-market-separated data. Once written, the artifact is immutable during runtime and the PAPER loop performs inference only. Runtime restarts never retrain it.

The model may use the Polymarket midpoint as a causal prior and correct its log-odds with external information. Every such inference records the exact PM snapshot identity and receive time; stale or already-repriced priors are rejected at action time.

The external feature state includes spot and perpetual L2 state, microprice, OFI, trade imbalance, cross-venue dispersion, fast returns and jump state, basis, funding, open-interest level/velocity, liquidation rates and Deribit volatility/term/skew features when they are causally available. Features with no historical labeled coverage are collected but are not assigned invented coefficients.

## Short-horizon repricing model

The lead/lag research dataset freezes one rich external feature cut and labels the subsequent Polymarket repricing at 100, 250, 500 and 1000 ms. Training is offline and explicit. This model is intended to estimate information lead for MAKE/CANCEL rather than settlement probability.

## Maker execution model

The Maker execution model is the only adaptive model in the PAPER runtime. It learns from the current run only and is refit periodically from canonical order/fill/markout evidence. It estimates censored fill/survival behavior, queue/funnel state, placement effects and fill-conditioned adverse markout.

Fill probability uses a declared Beta prior and current-run evidence. After at least 20 orders and 2 independent market clusters, the PAPER decision plane may use a conservative posterior lower bound instead of forcing the fill lower bound to zero. Evidence confidence is reported separately and never fabricates fills.

## Decision plane

All model outputs compete inside one coordinator. The action set is MAKE, TAKE, CANCEL, WITHDRAW or NOTHING. Cancellation may be triggered directly by the configured receive-time-causal external shock rule. No model owns capital, inventory, an OMS or a second ledger.
