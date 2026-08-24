# Polymarket System Watch: research, approval and model integration

This document is the operating contract for the hourly Polymarket System Watch scheduler.

## Non-negotiable invariant: one live champion

At every point in time there is exactly one authoritative live paper model:

- `main` contains the integrated production-quality code line;
- `config/live_champion.json` selects the single live entry point, configuration and run root;
- `paper-validated` identifies the exact `main` revision that passed the required public live-paper validation;
- the private paper server deploys `paper-validated`, never an arbitrary research branch or an unvalidated `main` revision.

A single champion does **not** mean collapsing every economic object into one undifferentiated forecast. The champion may contain structural, terminal-probability, relative-value, external-information and execution experts, but it must expose one orchestrator, one portfolio/risk layer and one execution path. Terminal probabilities must remain distinct from mark-to-market convergence signals, and both must remain distinct from fill and queue models.

Merely adding `paper_v5`, `paper_v6`, or another numerically newer implementation must not promote it. Promotion requires an explicit update of `config/live_champion.json` in an approved integration pull request.

## Scheduler responsibilities

On every scheduled cycle, System Watch must:

1. **Observe the current champion.** Record `main`, `paper-validated`, deployed revision, CI, live smoke, runtime health, PnL, drawdown, kill state, OOS eligibility, fill imbalance and strategy-level diagnostics.
2. **Inspect the research queue.** Identify active `research/*`, `experiment/*` and `diagnostic/*` work, its hypothesis, evidence status and whether it is stale, rejected, still collecting evidence or approved.
3. **Keep unapproved research isolated.** Unapproved model logic must not alter production intents, booked PnL, sizing, exposure, drawdown, kill switches or the live champion manifest.
4. **Evaluate candidates against the incumbent.** Use the same information set, market sample and time interval wherever possible. Compare executable net economics after spread, fees, slippage, queue/fill probability, latency, adverse selection, uncertainty and capital usage. In-sample improvement alone is not approval.
5. **Make an explicit research decision.** Record one of `REJECTED`, `MORE_EVIDENCE_REQUIRED`, `APPROVED_FOR_INTEGRATION` or `SHADOW_ONLY` with the supporting evidence and failure modes.
6. **Integrate every approved improvement.** Create a fresh `integration/*` branch from current `main`, port the reusable approved code, resolve overlap with the incumbent, remove or disable superseded paths, and connect the improvement to the existing champion interfaces rather than leaving a second complete model beside it.
7. **Validate the unified champion.** Run deterministic Release and Debug CI, mock integration tests, live-paper smoke, execution/risk checks, monitoring contracts and the applicable walk-forward/OOS and cost-stress gates.
8. **Promote and verify.** Merge at most one approved integration PR at a time, trigger post-merge validation, require `paper-validated` to advance to the merged revision, verify deployment and health, and report what changed and how the live system now acts.
9. **Fail closed.** A failed, ambiguous or stale gate leaves the incumbent champion live. The scheduler must never manufacture activity by lowering economic, risk or evidence thresholds merely to obtain trades.

The scheduler is therefore not only a monitor. It is the owner of the complete loop

```text
research -> evidence -> approval -> integration -> validation -> single live champion
```

## Branch policy

### `main`

`main` is the only authoritative integrated code line. It is not a research notebook and not a collection of alternative complete models. Normal bug fixes, execution hardening and infrastructure changes still enter through focused pull requests and must preserve the champion contract.

### `research/*`

Use for model hypotheses, estimators, features, alpha sleeves and parameter studies that have not yet passed the full approval gate. A research PR should normally remain a draft or be closed after its result is recorded. Approval of the idea does not make the research branch mergeable: approved code is consolidated on a new `integration/*` branch based on current `main`.

### `experiment/*` and `diagnostic/*`

Use for temporary forward tests, measurement workflows and fault isolation. Preserve conclusions in a PR, issue or durable document, then close the branch. Experimental workflow scaffolding and generated evidence are not merged merely to preserve history.

### `integration/*`

Use only after research approval. This branch is the staging area for the next unified champion. It must start from current `main`, link the source research evidence, adapt the candidate to the shared signal/intent/risk/execution contracts and delete or retire duplicated production paths.

### Shadow-only exception

Instrumentation may enter `main` before economic approval only when every condition below is satisfied and the PR is labelled `shadow-isolated`:

- it writes to separate state and telemetry;
- it cannot emit production intents or authenticated orders;
- it cannot book estimated rewards or hypothetical fills as realized PnL;
- it cannot change champion thresholds, sizing, exposure, drawdown, kill-switch or OOS gates;
- failure is non-blocking for trading but visible in diagnostics;
- deterministic tests enforce the separation;
- the PR states the measurement question and the decision that the evidence will support.

This exception exists to measure a candidate, not to smuggle an unapproved model into the live decision path.

## Research approval gate

A candidate can be marked `research-approved` only when the applicable evidence supports all of the following.

### Technical validity

- deterministic and replayable inputs and decisions;
- no look-ahead, resolution leakage, duplicated observations or hidden state reset;
- correct event-time alignment and restart persistence;
- unit, integration and regression coverage for the new behavior.

### Statistical validity

- chronological train/calibration/test separation with an appropriate embargo;
- comparison with the current champion on common data;
- uncertainty, multiple-testing and regime instability considered;
- enough observations or an explicit decision to collect more evidence rather than infer alpha from noise.

### Economic validity

- edge is computed at executable prices;
- all legs, fees, slippage, latency, queue/fill probability, adverse selection and capital time are included;
- paper PnL contains only simulated executions admitted by the documented execution model;
- normal and stressed costs are reported;
- improvement is not obtained by weakening an incumbent safety or admission gate without evidence.

### Portfolio and operational validity

- incremental contribution is measured after correlation and event concentration, not strategy-by-strategy in isolation;
- drawdown and worst-case open loss remain within the operating budget;
- state migration, rollback, telemetry and failure behavior are documented;
- real-money execution remains separately authorized and is never enabled by model approval alone.

Approval can concern a reusable component even when it does not become a standalone sleeve. The integration decision should choose the smallest coherent change that improves the unified champion.

## Mandatory integration procedure

For every approved research result, System Watch must perform the following sequence.

1. Create `integration/<research-slug>` from the latest `main`.
2. Link the research PR/commit and copy only reusable, reviewed implementation and tests.
3. Map the candidate into the existing semantic interface: terminal probability, structural constraint, relative-value expectation, external forecast, execution estimate or portfolio/risk input.
4. Use one shared model registry/orchestrator, one intent schema, one capital allocator, one risk state and one execution broker. Do not leave two independent full stacks competing for live control.
5. Reconcile duplicated features, estimators, configuration keys, state files and telemetry. Remove the superseded implementation or document a temporary compatibility path with a deletion condition.
6. Re-run incumbent-versus-candidate evidence on the integrated code, including ablations so that incremental alpha is identifiable.
7. Update `config/live_champion.json` only when the integration deliberately changes the live entry point/version. A new manifest version is a promotion decision, not a naming convention.
8. Open a non-draft PR to `main` carrying both `approved-for-integration` and `single-model-reviewed` labels.
9. Merge only after all required checks are complete and successful. The hourly automation merges at most one such PR per cycle and never uses an administrative bypass.
10. Explicitly dispatch CI, monitoring and live-paper smoke after an automation-token merge. The live server remains on the prior `paper-validated` revision until the new revision passes validation.
11. Verify `main == paper-validated == deployed HEAD` after the validation/deployment chain. If the chain fails, keep or restore the previous validated champion and publish the blocker.
12. Close the source research PR, delete short-lived research/integration branches when safe, and preserve the decision and evidence in durable history.

## Automation labels

The scheduler creates and recognizes these labels:

- `research-approved`: the research evidence is approved and semantic integration is now required;
- `approved-for-integration`: the consolidated implementation is approved for merge once checks pass;
- `single-model-reviewed`: the PR has been reviewed for duplicate models, configuration, state, telemetry and live routing;
- `shadow-isolated`: the change is measurement-only and cannot affect production decisions, PnL or risk.

`research-approved` belongs on research evidence. The two integration labels belong only on `integration/*` pull requests. Applying integration labels directly to `research/*`, `experiment/*` or `diagnostic/*` is a policy failure.

## Hourly report

Every run must state, even when no change is made:

- current `main`, `paper-validated` and deployed revision;
- current champion version, loop, config and run root;
- open research and integration queue;
- evidence reviewed and decision reached;
- code integrated, duplicate path removed and behavioral change;
- checks dispatched or blocked;
- live PnL/risk/OOS/health state;
- the next concrete research or integration action.

Silence is not evidence of health. A no-change run should say that the incumbent remains live and why no candidate was promoted.
