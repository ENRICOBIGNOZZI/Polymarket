# Development workflow

This repository uses `main` as the only authoritative integrated code line. The live paper entry point is selected explicitly by `config/live_champion.json`; code on another branch, or a numerically newer `paper_vN`, is not live until it passes the research, administrator approval, integration, validation and deployment lifecycle in [`SYSTEM_WATCH.md`](SYSTEM_WATCH.md).

The distributed automation architecture is documented in [`SCHEDULER_CONTROL_PLANE.md`](SCHEDULER_CONTROL_PLANE.md). Each workflow has exactly one job and one bounded responsibility.

## Branch roles

- `main`: stable, reproducible, unified paper champion. Every commit must build, pass deterministic tests and preserve a single portfolio/risk/execution path.
- `feat/*`, `fix/*`, `improve/*`, `ops/*`: short-lived focused implementation branches merged through a pull request.
- `research/*`: unapproved model hypotheses, features, estimators and parameter studies. They are evidence sources, not directly mergeable production models.
- `experiment/*`, `diagnostic/*`: temporary live-data experiments and measurement workflows. Record the result, then close without merge unless a separately reviewed shadow-only instrument is reusable.
- `integration/*`: the only branch class used to consolidate approved research into the current champion. It must start from current `main` and link the source evidence.
- `ci/*`, `tmp/*`, `noop`: disposable infrastructure branches. They must not remain active after their purpose is complete.

At most one broad `integration/*` branch should be active at a time. Smaller independent fixes should still use narrowly scoped pull requests.

## Research placement

Unapproved research belongs on `research/*`, `experiment/*` or `diagnostic/*`, not directly on `main`.

A limited exception exists for code labelled `shadow-isolated`. Such code may be merged only when tests prove that it writes separate telemetry/state and cannot emit production intents, book PnL, change sizing/exposure, alter drawdown or kill-switch behavior, modify OOS gates or submit authenticated orders. The exception exists for measurement, not premature promotion.

Approval of a research result does not authorize merging the research branch. The project administrator or an explicitly bounded integration task creates a new `integration/*` branch from current `main`, ports approved reusable code, resolves overlap with the incumbent, removes superseded paths and validates the resulting single champion.

## Single-champion invariant

- `main` contains one integrated codebase, not several complete live models.
- `config/live_champion.json` selects one loop, one config and one production run root.
- `paper-validated` is the exact `main` revision that passed live-paper validation.
- the private paper server deploys `paper-validated` only.
- specialized experts may coexist inside the champion, but terminal probability, relative value, structural constraints, execution estimates and portfolio risk retain distinct semantics.

Changing `config/live_champion.json` is a promotion action. It is allowed only from an `integration/*` PR carrying `approved-for-integration`, `single-model-reviewed` and `administrator-approved`.

## Merge gates

A pull request is mergeable only when all applicable conditions hold:

1. Release and Debug builds complete successfully.
2. Deterministic unit and mock integration tests pass.
3. New behavior has tests or an explicit reason why a deterministic test is impossible.
4. Configuration, state persistence, migration, rollback and failure behavior are documented.
5. Terminal-probability forecasts remain separate from mark-to-market relative-value signals.
6. Paper execution remains separate from authenticated real-money order submission.
7. No credentials, private keys, wallet secrets or generated runtime state are committed.
8. Externally dependent live-data checks are reported separately from deterministic CI.
9. Model changes report executable economics after spread, fee, slippage, depth, queue, latency, adverse-selection, uncertainty and capital costs.
10. Approved research is compared with the incumbent on common chronological data and integrated through `integration/*` rather than appended as a second complete stack.
11. An integration PR identifies and removes or explicitly retires superseded code, configuration, state and telemetry paths.
12. A live champion change updates the manifest explicitly and preserves rollback to the preceding `paper-validated` revision.
13. Automatic integration requires explicit `administrator-approved` on the exact reviewed PR.

## Pull-request lifecycle

1. Create the branch from current `main`.
2. Keep the PR focused and document the economic interpretation of every signal and cost.
3. Keep research PRs draft or close them after recording `REJECTED`, `MORE_EVIDENCE_REQUIRED`, `APPROVED_FOR_INTEGRATION` or `SHADOW_ONLY`.
4. For approved research, create a fresh `integration/*` PR that links the evidence and satisfies the single-model checklist.
5. Use squash merge for normal feature and integration work so `main` has one coherent commit per PR.
6. The dedicated integration scheduler may merge at most one non-draft, fully green `integration/*` PR carrying all three approval labels. It never uses an administrative bypass.
7. The integration scheduler performs only the merge and emits a repository-dispatch handoff.
8. The separate post-merge scheduler dispatches CI, monitoring and live-paper smoke for the exact merged SHA.
9. Merge success is not validation, deployment or health success. Promotion is complete only after `main == paper-validated == deployed HEAD` and runtime health passes.
10. Delete the head branch after merge or closure.

A branch must never remain alive merely to preserve history: commits, PR discussions and durable research notes are the historical record.

## Scheduler development

Every workflow under `.github/workflows` must be registered in `config/scheduler_registry.json` and contain one top-level job. The registry preserves unique authority:

- only `integration-merge` may merge an approved integration;
- only `post-merge-validation` may dispatch the post-merge validation bundle;
- only `paper-server-deploy` may deploy;
- the administrator supervisor is read-only.

Run:

```bash
python3 scripts/validate_scheduler_registry.py
```

before changing any workflow. CI rejects unregistered workflows, multiple jobs, duplicate privileged authority or mutation logic in the administrator supervisor.

## Safety boundary

The repository is a research and paper-trading system. A future authenticated broker must be a separate adapter with its own reconciliation, balance checks, cancel/replace logic, real-fill confirmation and production kill switches. Research approval, administrator approval, model integration, compilation or paper deployment is not sufficient authorization to trade real money.
