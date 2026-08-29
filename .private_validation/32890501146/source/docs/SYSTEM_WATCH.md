# Polymarket System Watch: distributed administration and model evolution

System Watch is a **control plane**, not one monolithic scheduler. The complete operating model is defined in [`SCHEDULER_CONTROL_PLANE.md`](SCHEDULER_CONTROL_PLANE.md) and registered in [`config/scheduler_registry.json`](../config/scheduler_registry.json).

The project administrator owns the evolution of the live system. Specialized schedulers execute bounded, auditable duties; none receives general authority over research, merging, validation, deployment and runtime at the same time.

## Non-negotiable invariant: one live champion

At every point in time there is exactly **one live champion**:

- `main` is the authoritative integrated code line;
- `config/live_champion.json` selects the single live entry point, configuration and run root;
- `paper-validated` is the exact `main` revision that passed public live-paper validation;
- the private paper server deploys `paper-validated`, never a research branch or arbitrary `main` revision.

A single champion does not collapse distinct economic objects. Structural constraints, terminal probabilities, mark-to-market relative value, external information and execution estimates retain their own semantics. They are combined through one shared model registry/orchestrator, one portfolio/risk allocator and one execution broker.

Creating `paper_v5`, `paper_v6` or a numerically newer experiment does not promote it. Promotion requires an explicit, rollback-safe integration decision.

## Distributed responsibility

The old all-in-one model-governance workflow has been removed. Responsibility is split as follows:

1. **Administrator Supervisor** observes the champion, workflow health, research queue, integration queue and blockers. It cannot approve, merge, dispatch validation or deploy.
2. **Research Policy** enforces branch, label, manifest and shadow-isolation rules. It cannot select a model or merge it.
3. **Research Queue** inventories evidence and reports what is rejected, collecting evidence, shadow-only or approved. It cannot make the approval decision.
4. **Integration Merge** may merge at most one approved integration PR at a time, only after explicit administrator approval and all required checks. It cannot perform research evaluation or deployment.
5. **Post-Merge Validation** dispatches CI, monitoring and live-paper validation for the exact merged SHA. It cannot merge or deploy.
6. **CI**, **Monitoring Validation** and **Live-Paper Validation** independently validate code, observability and paper behavior.
7. **Paper Server Deploy** deploys only `paper-validated`.
8. **Paper Server Health** observes the deployed system, processes, PnL/risk/OOS and observability evidence without modifying code.
9. Dedicated research workflows collect evidence for a specific hypothesis and remain isolated from production decisions.

The complete lifecycle remains:

```text
research -> evidence -> approval -> integration -> validation -> single live champion
```

The difference is that no individual scheduler owns the entire chain.

## Administrator authority

An integration can be merged automatically only when a non-draft `integration/*` PR carries all of:

- `approved-for-integration`;
- `single-model-reviewed`;
- `administrator-approved`.

`administrator-approved` is the explicit production-evolution decision. It is valid only for the exact reviewed integration PR and can be removed at any time before merge. No scheduler uses `--admin`, bypasses failed checks or lowers economic/risk thresholds to manufacture eligibility.

## Branch policy

### `main`

`main` contains integrated production-quality paper code. It is not a research notebook and not a collection of alternative complete models. Focused fixes and infrastructure changes still enter through pull requests and must preserve the champion contract.

### `research/*`

Unapproved research belongs here: hypotheses, estimators, features, alpha sleeves and parameter studies. A research PR is an evidence source, not a production model. It normally remains draft or closes after a durable decision is recorded.

### `experiment/*` and `diagnostic/*`

Use these for temporary forward tests, measurement workflows and fault isolation. Preserve the conclusion, then close. Generated evidence is history; temporary scaffolding is not merged merely to preserve it.

### `integration/*`

This is the only branch class for approved research integration. It must start from current `main`, link the source evidence, port only reusable reviewed code, reconcile overlap and remove or retire superseded paths.

### Focused implementation branches

`feat/*`, `fix/*`, `improve/*` and `ops/*` remain appropriate for bounded implementation, risk, execution and infrastructure changes that are not research promotion. They cannot change an existing `config/live_champion.json`; that remains an integration action.

## Keep unapproved research isolated

Unapproved model logic must not alter:

- production intents or admitted bundles;
- booked PnL;
- sizing or exposure;
- drawdown budgets or kill switches;
- OOS eligibility;
- authenticated execution;
- `config/live_champion.json`.

### Shadow-only exception

Reusable measurement code may enter `main` before economic approval only with `shadow-isolated` and deterministic proof that:

- it writes separate files, state and telemetry;
- it cannot emit production intents or authenticated orders;
- hypothetical fills, rewards and markouts are not booked as realized PnL;
- production thresholds, sizing, exposure, drawdown, kill switches and OOS gates are unchanged;
- failure is visible but cannot corrupt or block the production decision path.

Shadow is for measurement, not hidden promotion.

## Research approval gate

`research-approved` may be applied only when the applicable evidence supports the following.

### Technical validity

- deterministic/replayable inputs and decisions;
- no look-ahead, resolution leakage, duplicate observations or hidden reset;
- correct event-time alignment and persistent restart behavior;
- unit, integration and regression coverage.

### Statistical validity

- chronological train/calibration/test separation with embargo where needed;
- common information set and sample for incumbent-versus-candidate comparison;
- uncertainty, multiple testing and regime instability considered;
- enough observations, otherwise `MORE_EVIDENCE_REQUIRED`.

### Economic validity

- executable prices rather than midpoint claims;
- spread, fee, slippage, depth, queue/fill probability, latency, adverse selection, uncertainty and capital time included;
- normal and stressed costs reported;
- no improvement obtained by weakening an incumbent safety gate without evidence.

### Portfolio and operational validity

- incremental value measured after correlation and event concentration;
- drawdown and worst-case open loss remain within the operating budget;
- state migration, rollback, telemetry and failure behavior documented;
- model approval never authorizes real-money execution.

Research decisions are recorded as `REJECTED`, `MORE_EVIDENCE_REQUIRED`, `APPROVED_FOR_INTEGRATION` or `SHADOW_ONLY`.

## Mandatory integration procedure

For every approved result:

1. Create `integration/<research-slug>` from the latest `main`.
2. Link the research PR, branch or commit.
3. Port only reusable reviewed code and tests.
4. Map the candidate into the existing semantic interface: structural, terminal, relative-value, external-information, execution or portfolio/risk input.
5. Preserve one shared model registry/orchestrator, intent schema, allocator, risk state and broker.
6. Remove duplicated or superseded implementation/config/state/telemetry, or document a short compatibility path with a deletion condition.
7. Re-run integrated incumbent-versus-candidate evidence and ablations.
8. Update `config/live_champion.json` only when the entry point/version deliberately changes.
9. Open a non-draft integration PR with `approved-for-integration`, `single-model-reviewed` and `administrator-approved`.
10. Require Release, Debug, deterministic tests, monitoring validation and live-paper smoke to succeed.
11. Merge at most one administrator-approved integration PR at a time; no second integration starts while `main != paper-validated`.
12. Hand the exact merged SHA to the separate post-merge validation scheduler.
13. Require `main == paper-validated == deployed HEAD` before the promotion is considered complete.
14. Close source research and delete short-lived branches when safe.

## Fail-closed behavior

A failed, missing, ambiguous or stale gate leaves the incumbent champion live. Integration, validation and deployment are separate state transitions:

```text
merged != validated != deployed != healthy
```

The supervisor reports these states but cannot repair them by bypassing the responsible scheduler.

## Automation labels

- `research-approved`: evidence is approved and requires semantic integration;
- `approved-for-integration`: the consolidated implementation passed the research/integration review;
- `single-model-reviewed`: one orchestrator/config/risk/execution path has been verified;
- `administrator-approved`: the project administrator authorizes this exact integration;
- `shadow-isolated`: measurement-only code cannot affect production decisions, PnL or risk.

Research labels belong on research evidence. Integration and administrator labels belong only on `integration/*` PRs.

## Reporting

The Administrator Supervisor publishes an hourly report containing:

- current `main`, `paper-validated` and validation relation;
- champion version, loop, config and run root;
- latest state of every registered scheduler;
- open research, approved-research and integration queues;
- conflicting integration candidates;
- control-plane blockers and warnings.

Silence is not evidence of health. A no-change run must explain why the incumbent remains live.
