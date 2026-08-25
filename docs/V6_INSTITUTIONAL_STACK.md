# V6 institutional execution and risk hardening

This revision keeps V6 paper-only and preserves the single-operator governance boundary. It does not add an authenticated order path.

## Execution economics

Taker decisions no longer assume that touch price plus a fixed scalar slippage is sufficient. `scripts/v6_execution_model.py` provides shared primitives for:

- full displayed-depth walking with no extrapolated liquidity;
- market-specific fee descriptors where available;
- residual slippage that increases with spread, short volatility and participation and decreases with liquidity;
- passive fill-hazard diagnostics as a function of queue ahead, contra flow and quote lifetime;
- conditional adverse-selection penalties;
- uncertainty lower-confidence-bound executable edge;
- bounded short-horizon mean-variance sizing rather than binary Kelly for micro/relative-value signals.

The maker paper engine additionally rejects quotes whose displayed queue/order ratio exceeds the configured cap. Queue priority remains reset on cancel/replace.

## Micro taker

`scripts/v6_micro_taker_institutional.py` replaces the operational simple ridge taker with a causal, robust, exponentially weighted short-horizon model. Its state includes L1/L5 imbalance, YES/NO microprice consistency, depth curvature, short momentum, short volatility, normalized spread and liquidity.

Targets retain the existing no-lookahead `v6_micro_target.py` contract. The model uses Huber reweighting, publishes prediction uncertainty and rejection reasons, ranks on uncertainty-adjusted executable edge, walks the intended size through displayed depth and rechecks edge after sizing.

## Dynamic local factor

`scripts/v6_dynamic_factor_intents.py` replaces heterogeneous level factor estimation on the V6 intent path. The latent factor is estimated on aligned logit returns with exponentially weighted loadings. Factor-neutral residual returns are integrated only after common-factor removal; mean reversion remains subject to the existing AR stability tests and BH-FDR control.

The resulting basket remains a maker relative-value intent handled by the existing multi-leg broker. It is not reinterpreted as terminal event probability.

## Global scenario risk

`scripts/v6_global_risk.py` sits above the capital-isolated sleeves. It reconciles durable maker, multi-leg, micro-taker, hard-arbitrage and external-engine state and enforces:

- global drawdown;
- global gross exposure;
- sleeve-shocked scenario loss;
- propagation of any child kill;
- fail-closed missing/stale state after bounded startup grace.

`global_kill.flag` is sticky across restarts. The runtime never clears it automatically.

## Research and deployment boundary

The research and CI workflows compile and test the new stack and collect live public-data evidence. Promotion still follows the repository's existing research -> approved integration -> exact-SHA CI/monitoring/live-paper validation -> `paper-validated` -> deployment chain.

Authenticated real-money execution remains disabled. A future live adapter still requires separately reviewed user-channel reconciliation, balances/allowances, order acknowledgement state, FOK/FAK behavior, unmatched-leg controls and production kill switches.
