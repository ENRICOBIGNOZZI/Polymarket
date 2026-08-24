# Continuous alpha research scheduler

The alpha scheduler is a research controller, not a parameter randomizer and not a production-order adapter.

## Objective

Every cycle asks a narrow question:

> Does a pre-declared challenger add executable, diversified edge relative to the current family champion, and does that improvement survive chronological out-of-sample evaluation after costs?

The controller currently covers the B1 pair-stat-arbitrage and B2 sparse-PCA families. The configuration is in `config/alpha_research.json`; the executable controller is `scripts/alpha_research.py`.

## Cycle

The scheduled workflow runs every six hours on the private paper node.

1. It selects a bounded rotating subset of challengers. Champions are always re-run as contemporaneous references.
2. It invokes only fixed, validated scanner binaries and parameter names. No generated shell is executed.
3. It measures maker-entry/taker-exit net edge, positive executable notional, concentration, stability and a diversified expected-edge-dollar score.
4. It compares each challenger with the contemporaneous champion in the same research cycle.
5. A screen winner becomes `shadow_required`; it is not production alpha.
6. A challenger can become `promotion_ready` only when a separate challenger-specific walk-forward report passes the normal OOS gate, improves normal and stressed mean return, does not increase drawdown, is stable across folds, and survives a multiplicity adjustment.
7. The workflow publishes a JSON/Markdown audit trail and opens or updates a promotion ticket. If the report was produced from the exact current `main` SHA, it also opens a draft PR that changes the versioned champion configuration. It never merges or deploys that PR.

The state machine is:

```text
scanner_error
screen_rejected
shadow_required
OOS_rejected
promotion_ready
```

The B1/B2 paper runtime reads the versioned champion from `config/alpha_research.json` through `scripts/alpha_config_env.py`. The only path from `promotion_ready` to production is the automatically prepared **draft** pull request, candidate-config live smoke, review/merge, the normal public paper smoke, the `paper-validated` ref, and the existing rollback-safe deployment path.

## Why promotion is deliberately slow

Repeatedly lowering a z-score or edge threshold until a trade appears is multiple testing, not alpha discovery. The scheduler therefore:

- declares the challenger set before observing the cycle;
- limits how many challengers are tested per cycle;
- keeps executable costs in the scanner metric;
- penalizes concentration;
- requires challenger-specific chronological OOS evidence;
- applies a familywise p-value adjustment;
- never uses the scheduled scan itself to book paper PnL or change production;
- refuses to prepare a PR when the research SHA differs from current `main`;
- records the previous champion as a rollback challenger in every proposed promotion.

## Artifacts

The private paper node writes:

```text
runs/paper_v4_live/alpha_research/latest.json
runs/paper_v4_live/alpha_research/latest.md
runs/paper_v4_live/alpha_research/history.jsonl
runs/paper_v4_live/alpha_research/cycles/<timestamp-cycle>/
```

The GitHub workflow uploads the latest report and publishes a public-data-only checkpoint to `telemetry/latest-alpha-research.json`.

## Current limitation

A screen result is not a backtest and does not establish profitability. Challenger-specific shadow ledgers under `runs/paper_v4_live/alpha_research/oos/` must be produced before promotion can occur. Until then, every apparent improvement remains `shadow_required`.
