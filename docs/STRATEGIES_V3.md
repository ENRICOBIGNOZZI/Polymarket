# Polymarket V3 strategy architecture

V3 keeps alpha generation and execution separate. No authenticated order submission is part of this architecture.

## Strategy A — Structural arbitrage

Executable constraints rather than forecasts:

- binary YES/NO parity;
- complete non-augmented NegRisk buy-all-YES baskets;
- NegRisk `NO_i -> YES_{j != i}` conversion relationships;
- live bid/ask, displayed depth, taker fees and slippage are applied before an opportunity is considered executable;
- conversion diagnostics remain explicitly pre-gas/pre-latency until those costs are measured rather than guessed.

Implementation: `polymarket_negrisk_arb` plus pair-parity diagnostics in the main engine.

## Strategy B — Statistical arbitrage

Multi-horizon relative-value alpha. It is not a terminal probability forecast.

For candidate markets `i,j`, the strategy works with timestamp-aligned logit probabilities and estimates

`logit(p_j,t) = a + beta logit(p_i,t) + r_t`.

The residual is screened with an ADF-style one-lag regression

`Delta r_t = c + gamma r_{t-1} + eps_t`,

with `phi = 1 + gamma`. Candidate trades require:

- sufficient common timestamp observations;
- economically/statistically plausible relation (same event, semantic relation, or a much stricter latent-correlation gate);
- stable hedge ratio and residual dynamics across split samples;
- negative and sufficiently significant residual feedback;
- finite half-life below the configured maximum;
- current residual z-score beyond the entry threshold.

The expected residual convergence is mapped into expected mark-to-market moves for both legs. This produces `expected_mark_move`, not `P(outcome=YES)`. Therefore Kelly sizing based on binary terminal payoff is not used for this strategy.

The scanner evaluates several windows and reports both:

- conservative taker-entry/taker-exit economics;
- maker-entry/taker-exit economics as a diagnostic, without assuming a passive fill.

Implementation: `polymarket_stat_arb`.

## Execution — conservative paper maker

`polymarket_maker_paper` is an execution simulator, not a third forecasting model.

Initial implementation deliberately biases against optimistic fills:

- passive buy joins the displayed best bid;
- displayed size already at that price is recorded as `queue_ahead`;
- touching the limit is not a fill;
- a maker order is filled only after strict trade-through evidence in a later market snapshot;
- if the live best bid moves above our simulated quote, the stale order is cancelled instead of assuming queue priority;
- entry has zero maker fee;
- exits are simulated as taker, including book walk, slippage and taker fee;
- orders and positions are persisted across ticks.

This is intentionally harsher than a fill-probability model. A calibrated queue/fill hazard model can replace it only after enough empirical paper-order data have been collected.

## Terminal-probability sleeve

The original terminal forecasting engine remains available, but `config/paper_v3.json` assigns zero ensemble weight to `micro` and the legacy index-aligned `pca` expert. Structural graph/external terminal information can still be used there.

This prevents short-horizon price signals from being interpreted as terminal probabilities and prevents Kelly from being applied to the wrong object.

## Recommended paper cadence

- maker microstructure loop: approximately every 10 seconds;
- structural-arbitrage scan: approximately every 30 seconds;
- statistical-arbitrage refit: approximately every 15 minutes;
- terminal-probability scan: slower, or event-driven when external information changes.

Performance should be reported by strategy and execution mode separately before any portfolio-level aggregation.
