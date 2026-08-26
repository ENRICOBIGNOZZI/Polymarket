# Polymarket System Watch

System Watch is the V7 control plane. It separates research, promotion decisions, merging, exact-SHA validation, deployment and runtime health so that no scheduler can silently turn an experiment into the live PAPER system.

The machine-readable authority is `config/scheduler_registry.json`; the current operator envelope is `config/operator_directives.json`.

## Canonical state

There is one canonical runtime generation and one live champion:

```text
main
  -> config/live_champion.json (V7)
  -> exact-SHA validation
  -> paper-validated
  -> private deployment
  -> server-health
```

Promotion is complete only when the intended revision has converged through those states. In particular:

```text
merged != validated != deployed != healthy
```

A failed or ambiguous downstream gate does not authorize substituting another revision.

## Responsibility split

1. **Administrator Supervisor** observes repository/control-plane state and reports blockers. It cannot merge, validate or deploy.
2. **Research Policy** enforces branch, provenance, operator-authority and isolation rules.
3. **Research Queue** inventories V7 research and evidence without production mutation.
4. **Promotion Controller** evaluates integration candidates against objective technical/economic gates and may issue the ephemeral `autonomous-promotion-approved` authorization.
5. **Integration Merge** is the only merge authority. It revalidates the selected exact head and may merge only a controller-authorized candidate.
6. **Post-Merge Validation** dispatches CI, monitoring and V7 PAPER validation for the exact merged SHA.
7. **CI**, **Monitoring** and **V7 Live-Paper Validation** independently validate code, observability and PAPER runtime behavior.
8. **Paper Server Deploy** deploys only the exact `paper-validated` revision.
9. **Paper Server Health** observes exact deployed revision, process ownership, market data, execution/risk state and monitoring.
10. Dedicated V7 research workflows collect prospective evidence and have no merge/deploy/authenticated-execution authority.

## Research lifecycle

Unapproved model work belongs on `research/*`, `experiment/*` or `diagnostic/*`. It cannot change the live champion, book production PnL, alter production sizing/risk or submit authenticated orders.

Research decisions are recorded with one of:

```text
REJECTED
MORE_EVIDENCE_REQUIRED
APPROVED_FOR_INTEGRATION
INTEGRATION_READY
SHADOW_ONLY
```

A positive governance verdict must bind the exact source head. If the source changes, the approval is stale.

## Integration lifecycle

A canonical integration uses an `integration/*` branch based on current `main` and binds its source in the PR body:

```text
Source research PR/branch/commit: #<number> / <research-branch> / <40-char-sha>
```

For economic surfaces, the Promotion Controller additionally requires machine-readable evidence bound to the source code and the configured OOS/economic gates. Evidence may not be pooled across code revisions or incompatible test windows.

The Promotion Controller does not merge. The Integration Merge workflow does not make an independent promotion decision. Authorization is re-evaluated each cycle and is invalidated by head/base/source drift.

## Exact-SHA validation

After an integration merge, Post-Merge Validation dispatches:

```text
ci.yml
monitoring.yml
v7-live-paper-validation.yml
```

for the same exact `main` SHA. V7 live-paper validation alone may advance `paper-validated`, and only after it proves that the checked-out revision equals current `main` and has merged-PR provenance.

## Deployment and health

The deployment workflow consumes `paper-validated`; it does not select a model or an arbitrary `main` commit. Server Health then verifies the deployed V7 SHA and the canonical runtime/monitoring contracts.

The convergence target is:

```text
main == paper-validated == deployed HEAD
```

plus a successful fresh server-health observation for that deployed revision.

## Evidence requirements

Model promotion is based on executable economics rather than theoretical quote edges. Depending on the strategy, evidence includes:

- authoritative fees and executable depth;
- slippage and capital-time costs;
- queue/fill probability and partial fills;
- adverse markouts and toxicity;
- joint multi-leg completion and unwind losses;
- chronological OOS windows and dependence-aware inference;
- drawdown, stability, data health and incremental utility.

Zero trades is a valid research outcome. Gates are not weakened merely to manufacture activity.

## Single-writer invariant

The live runtime has one state owner, one broker authority and one execution ledger. `runtime_singleton_launcher.py` owns the repository-wide runtime lock and process-group draining. Deployment/runtime handoff must not leave descendants from a preceding owner alive.

## Fail-closed rules

The control plane does not resolve failure by bypassing authority boundaries. Missing checks, stale evidence, mismatched SHAs, unhealthy data, duplicate runtime ownership or inconsistent monitoring remain blockers until the responsible component is corrected.

Authenticated real-money execution is not part of this control plane and remains disabled.
