# Alpha Factory and control plane

The Polymarket paper system has three supervisory layers with separate authority.
They are deliberately not one self-modifying process.

## 1. Runtime and System Watch

The existing paper loop owns market-data collection, scanners, intent admission,
paper execution, persistence, risk limits and operational action reports.
`model-governance.yml` owns the single-champion integration lifecycle. It may
merge at most one fully approved `integration/*` pull request, and only after
its required checks are complete.

Neither layer may reinterpret a failed gate as permission to trade.

## 2. Alpha Factory

`alpha-factory.yml` runs hourly after the latest live-smoke and forward-maker
windows. It reads only persisted evidence from the `telemetry` branch and:

1. diagnoses the signal-to-PnL funnel;
2. evaluates the unified paper ledger under purged walk-forward testing;
3. reconstructs both 1.5x and 2.0x cost stress where the ledger permits it;
4. applies block-bootstrap evidence and Benjamini-Hochberg FDR control;
5. tracks consecutive passes in a candidate registry;
6. compares forward maker policies using independent future windows;
7. emits a prioritized, falsifiable next-experiment queue;
8. recommends at most one paper canary or integration candidate;
9. recommends rollback if an active canary loses stressed profitability or
   violates drawdown limits.

A recommendation is not a live promotion. The scheduled workflow cannot edit
`config/live_champion.json`, move `paper-validated`, deploy the server, submit an
order or access trading credentials.

The promotion path remains:

```text
research evidence
  -> approved research
  -> one integration/* PR
  -> Release and Debug CI
  -> monitoring validation
  -> live paper smoke
  -> paper-validated
  -> existing gated paper deployment
  -> server health evidence
```

Zero candidates, zero trades and zero promotions are valid outcomes.

## 3. Meta-supervisor

`control-plane.yml` is the coordinator above System Watch and Alpha Factory. It
runs hourly and models the workflow dependency graph explicitly:

```text
CI -----------+
              +--> V4 live paper smoke --> paper-validated --> deploy chain
Monitoring ---+

CI --> forward maker research --+
V4 live paper smoke -------------+--> Alpha Factory

CI + Monitoring + V4 smoke --> Model Governance
```

For every workflow it records the latest default-branch run, covered commit,
age, status, conclusion and dependencies. It can dispatch only this allowlist:

- `ci.yml`;
- `monitoring.yml`;
- `v4-live-smoke.yml`;
- `forward-maker-research.yml`;
- `alpha-factory.yml`;
- `model-governance.yml`.

It dispatches at most three workflows per cycle, respects a cooldown after
failures, never duplicates an in-progress run and never starts a dependent node
before its prerequisites are healthy.

It is structurally forbidden from dispatching `deploy-paper-server.yml` or
`server-health.yml`. Deployment remains exclusively event-driven by successful
paper validation, while private server health keeps its own scheduled and
post-deploy checks.

## Champion and challenger invariants

`config/live_champion.json` is the only live paper champion manifest.
`telemetry/alpha-factory-state.json` is a research registry, not a second live
configuration. It may contain many observed challengers but at most one canary
recommendation and at most one active canary identifier.

A challenger cannot be integration-ready unless all of the following hold:

- enough finalized OOS trades;
- positive normal, 1.5x-cost and 2.0x-cost PnL;
- drawdown and profit-factor gates;
- sufficient active folds with a strict majority positive;
- block-bootstrap significance after FDR correction;
- a frozen production threshold;
- repeated independent passes;
- positive incremental utility versus the incumbent;
- certification that it fits the single orchestrator, allocator, risk and
  execution path.

Forward quote policies remain shadow-only until their cost-stress surface and
paired-fill evidence are identified. Estimated liquidity rewards are not booked
as certain revenue.

## Failure behavior

The complete control plane fails closed:

- stale telemetry produces `DEGRADED_STALE_EVIDENCE`, not a threshold change;
- failed or outdated prerequisites are repaired before downstream workflows;
- a diverged `paper-validated` ref is a critical condition;
- missing challenger evidence blocks promotion;
- stressed PnL deterioration or drawdown violation triggers a rollback
  recommendation;
- no component can disable the runtime kill switch or the 15% operating
  drawdown constraint;
- no component enables authenticated real-money execution.

The durable evidence files are published to the `telemetry` branch and retained
as GitHub Actions artifacts:

```text
telemetry/latest-alpha-factory.json
telemetry/latest-alpha-factory.md
telemetry/alpha-factory-state.json
telemetry/latest-control-plane.json
telemetry/latest-control-plane.md
```
