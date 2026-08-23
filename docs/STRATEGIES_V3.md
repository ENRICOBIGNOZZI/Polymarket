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

Multi-horizon relative-value alpha. This strategy produces expected mark-to-market convergence, not a terminal probability, and therefore does not use binary-outcome Kelly sizing.

### B1 — Pair residual sleeve

For candidate markets `i,j`, timestamp-aligned logit probabilities are used to estimate

`logit(p_j,t) = a + beta logit(p_i,t) + r_t`.

The residual is screened with an ADF-style one-lag regression

`Delta r_t = c + gamma r_{t-1} + eps_t`,

with `phi = 1 + gamma`. Candidate trades require sufficient common timestamps, an economically/statistically plausible relationship, stable hedge ratio/residual dynamics across split samples, negative residual feedback, finite half-life and a sufficiently extreme current residual z-score. Same-event and semantic pairs receive economically motivated gates; unrelated latent pairs require much stronger return correlation and reversion evidence.

Several horizons are fit and the expected residual convergence is mapped into a two-leg expected mark move. Both taker-entry/taker-exit and maker-entry/taker-exit economics are reported separately.

Implementation: `polymarket_stat_arb`.

### B2 — PCA / factor-residual sleeve

This is the timestamp-aligned replacement for the old index-aligned PCA signal.

- historical YES probabilities are bucketed by their actual timestamps and transformed to logit space;
- a sparse panel is retained rather than pretending different observation times are synchronous;
- pairwise correlations use only common timestamps;
- the leading factor directions are estimated from that correlation structure;
- at each timestamp factor scores are projected using only markets observed at that timestamp;
- each market's idiosyncratic residual is tested separately for mean reversion, half-life and split-sample stability;
- an extreme current factor residual is converted into an expected mark-to-market move over approximately one half-life;
- taker and maker-entry economics are costed separately.

This is a timestamp-aware sparse-panel factor/PCA approximation, not a claim of exact balanced-panel PCA.

Implementation: `polymarket_pca_stat_arb`.

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

This is intentionally harsher than a fill-probability model. A calibrated queue/fill hazard model should replace it only after enough empirical paper-order data have been collected.

## Terminal-probability sleeve

The original terminal forecasting engine remains available, but `config/paper_v3.json` assigns zero ensemble weight to `micro` and the legacy `pca` expert. Structural graph/external terminal information can still be used there.

This prevents short-horizon price signals from being interpreted as terminal probabilities and prevents Kelly from being applied to the wrong object.

## Recommended paper cadence

- maker microstructure loop: approximately every 10 seconds;
- structural-arbitrage scan: approximately every 30 seconds;
- pair and PCA statistical-arbitrage refits: approximately every 15 minutes;
- terminal-probability scan: slower or event-driven when external information changes.

Performance must be reported separately for Strategy A, Strategy B1, Strategy B2 and maker execution before any portfolio-level aggregation.
