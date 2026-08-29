# V4 roadmap: execution realism and research infrastructure

V4 must improve the credibility of paper results without weakening the safety boundary of the repository. It remains read-only with respect to Polymarket order submission.

## Design principles

1. Keep terminal-event probability forecasts separate from mark-to-market relative-value signals.
2. Evaluate every opportunity at executable prices and attribute every cost explicitly.
3. Never loosen thresholds merely to manufacture trades.
4. Persist enough event-time state to reproduce every signal, order decision and simulated fill.
5. Treat live API checks as external integration evidence, not as a substitute for deterministic tests.
6. Make the risk and execution layers independent from any one alpha model.

## Workstream 1 — event-time market data

Build a normalized event-time store for:

- Gamma market and event metadata;
- CLOB book snapshots with exchange timestamps and local receipt timestamps;
- public trades used for queue-depletion evidence;
- fee schedules and market-specific execution descriptors;
- market closure and resolution state;
- explicit data-quality flags for stale, missing, crossed or inconsistent books.

The store must support deterministic replay. Statistical bars may be derived from event-time data, but the raw sequence must remain available for execution tests.

## Workstream 2 — execution realism

Replace the current conservative all-or-nothing maker assumptions with a testable state machine that still defaults to pessimism:

- latency between signal, quote decision and simulated venue arrival;
- queue-ahead updates from trades and observable book changes;
- partial fills and residual order quantity;
- cancel/replace latency and stale-order exposure;
- trade-through, price improvement and adverse-selection diagnostics;
- multi-level book walking for taker exits;
- per-leg atomicity assumptions for baskets stated explicitly;
- restart reconciliation for pending orders, reserved cash and open positions.

Every fill must carry a provenance record containing the triggering observations and model assumptions.

## Workstream 3 — alpha research protocol

### Structural arbitrage

- retain binary parity and complete NegRisk basket checks;
- measure rather than guess conversion gas, latency and failure costs before calling conversion opportunities executable;
- distinguish hard algebraic violations from incomplete or augmented event sets.

### Pair and factor statistical arbitrage

- use event-time aligned histories and strict common-observation rules;
- compare rolling PCA with dynamic-factor and online-subspace alternatives;
- estimate convergence, half-life and stability out of sample;
- require factor-neutral hedge baskets and report hedge error on every candidate;
- maintain separate taker/taker and maker-entry/taker-exit economics.

### Terminal information

- preserve the generic external-signal interface;
- add source-specific staleness, calibration and reliability tracking;
- evaluate global, category and market-level calibration hierarchically;
- never feed short-horizon microstructure or relative-value outputs into terminal Kelly sizing.

## Workstream 4 — out-of-sample evaluation

Introduce a research ledger that records model version, configuration hash, input window, decision timestamp and realized evaluation horizon.

Required reports include:

- candidate funnel: raw dislocations, cost-positive signals, risk-admissible orders and simulated fills;
- gross edge, spread, fees, slippage, uncertainty penalty and net edge separately;
- fill ratio, time-to-fill, queue consumed and post-fill adverse selection;
- turnover, exposure, concentration, PnL, peak equity and drawdown;
- terminal Brier score and calibration only for genuine terminal-probability models;
- walk-forward and purged holdout results with no threshold selection on the evaluation segment.

A zero-trade result is valid evidence when all executable edges are negative.

## Workstream 5 — portfolio and risk

Move from independent sizing toward a joint constrained allocator while retaining hard safety limits:

- event and latent-event-cluster concentration;
- gross, market and strategy exposure limits;
- liquidity and unwind-capacity constraints;
- scenario loss and remaining drawdown budget;
- covariance-aware allocation for relative-value baskets;
- separate capital budgets for structural, pair, factor and terminal sleeves;
- persistent kill-switch and restart-safe deleveraging state.

The 15% drawdown target remains an operating constraint, not a deterministic guarantee.

## Workstream 6 — observability

Expose Prometheus-compatible metrics and provide a small Grafana dashboard covering:

- data freshness, API errors and scan latency;
- markets discovered, tradable markets and rejected-data counts;
- raw/costed/net signal counts by strategy;
- maker orders, queue ahead, fills, cancellations and time-to-fill;
- cash, reserved cash, gross exposure, event exposure, PnL and drawdown;
- model version, configuration hash and last successful persistence checkpoint.

CSV and JSON logs remain the durable research record; the dashboard is an operational view, not the accounting source of truth.

## Merge plan

V4 should be delivered through small reviewable commits or pull requests in this order:

1. deterministic event-time store and replay fixtures;
2. execution state machine and reconciliation tests;
3. research ledger and out-of-sample reports;
4. alpha upgrades behind configuration flags;
5. joint portfolio/risk allocator;
6. metrics exporter and Grafana assets;
7. removal of temporary compatibility paths and final documentation audit.

## Definition of done

V4 is mergeable only when:

- Release and Debug CI pass;
- deterministic replay reproduces signals and simulated fills exactly;
- restart tests preserve cash, positions, pending orders, peak equity and kill state;
- every strategy reports a complete gross-to-net cost decomposition;
- forward evaluation is genuinely out of sample and does not force trades;
- Grafana metrics reconcile with persisted ledger totals;
- no credential handling or authenticated real-money submission code is present.
