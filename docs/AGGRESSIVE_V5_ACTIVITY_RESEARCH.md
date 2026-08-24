# Aggressive V5 activity redesign

## Observed failure mode

The current V5 public-data smoke can report a healthy runtime while exercising only a graph child directly. The five generated child configurations are validated, but the smoke does not require all five sleeves to execute a paper cycle. In the observed snapshot, 120 discovered markets produced only two engine-tradable markets, pair stat-arb produced zero fitted models, and PCA produced mean-reverting residuals but zero accepted hedges.

## Design objective

Increase rational paper-live activity without admitting negative expected value after explicit fees and slippage. Aggressiveness is implemented through wider model coverage, lower but still positive net-edge gates, more frequent scans, partial-hedge sizing, and a 500-market universe. The authenticated real-money boundary remains unchanged.

## Required changes

1. Run micro, PCA, graph, semantic, and external sleeves continuously on the server and execute all five in the public smoke, rather than validating configurations only.
2. Separate model eligibility from immediate two-sided execution. A usable YES book can support forecasting; an order is still admitted only on a genuinely executable side.
3. Replace PCA's binary semantic-only and 20% hedge-error rejection cascade with a quantitative latent-factor path. Partial hedges are accepted only when residual displacement, stability, hedge error, and post-cost economics pass explicit bounds; weaker hedges receive smaller size.
4. Relax pair-stat-arb sample, correlation, stability, z-score, and mean-reversion gates while retaining post-cost execution checks.
5. Expand all live paper sleeves to 500 markets, lower the liquidity floor, increase scan cadence, and publish a per-cycle admission funnel.

## Risk invariants

- Paper-only execution remains mandatory.
- Every submitted paper order must retain positive modeled net edge after protocol fees, slippage, and configured uncertainty penalty.
- Global and sleeve drawdown kill switches remain active at 15%.
- Per-market, per-event, gross-exposure, cash, and order-book depth limits remain active.
- Structural arbitrage and multileg execution continue to use executable prices rather than mid-price opportunities.
