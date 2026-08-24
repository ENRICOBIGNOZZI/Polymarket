## What changes

Describe the implementation and the economic/statistical object it estimates or controls.

## Lifecycle status

Select exactly one primary status.

- [ ] Normal feature, bug fix, execution/risk, documentation or infrastructure change
- [ ] Research/experiment/diagnostic only — evidence source; close without direct merge
- [ ] Shadow-only instrumentation — requires `shadow-isolated`
- [ ] Automatic paper-champion integration — requires an `integration/*` branch and a numbered source research PR; no manual approval labels are required

## Research evidence

Required for research, shadow and integration work.

- Source research PR/branch/commit:
- Hypothesis or measurement question:
- Current champion baseline:
- Candidate specification:
- Common chronological sample / information set:
- Executable-cost assumptions:
- Normal and stressed result:
- Decision: `REJECTED` / `MORE_EVIDENCE_REQUIRED` / `INTEGRATION_READY` / `SHADOW_ONLY`

## Change type

- [ ] Feature or model change
- [ ] Bug fix
- [ ] Execution or risk change
- [ ] Experiment or diagnostic only
- [ ] Documentation or infrastructure

## Validation

- [ ] Release build succeeds
- [ ] Debug build succeeds
- [ ] Deterministic unit and mock integration tests pass
- [ ] New behavior is covered by tests, or the limitation is documented
- [ ] Live-data evidence is clearly separated from deterministic CI
- [ ] Costs, spread, slippage, depth, queue/fill probability, latency, adverse selection, uncertainty and capital usage are handled at executable prices where relevant
- [ ] Candidate and incumbent are compared on common chronological data without look-ahead or resolution leakage
- [ ] Walk-forward/OOS and cost stress are reported when the change claims economic improvement

## Single champion and model semantics

Required for every integration candidate.

- [ ] The candidate is connected to the existing expert/signal/intent interfaces rather than left as a second complete live stack
- [ ] There remains one model orchestrator/registry, one live config, one portfolio/risk allocator and one execution broker
- [ ] Terminal probabilities are not confused with mark-to-market relative-value signals
- [ ] Structural, external-information and execution estimates retain their correct economic meaning
- [ ] Duplicated or superseded implementation, configuration, state and telemetry paths are removed or given a documented deletion condition
- [ ] Incremental alpha is shown with an ablation against the incumbent champion
- [ ] `config/live_champion.json` is unchanged, or its promotion change is explicit and rollback-safe
- [ ] The previous `paper-validated` revision remains the rollback target until post-merge live smoke succeeds

## Automatic paper promotion

For non-draft `integration/*` PRs, the scheduler is the promotion authority.

- [ ] The PR links `Source research PR/branch/commit: #<number>`
- [ ] The source research PR has green Release, Debug and research-policy checks
- [ ] The integration PR has green Release, Debug, monitoring, research-policy and live-paper checks
- [ ] No `administrator-approved`, `approved-for-integration` or `single-model-reviewed` label is required
- [ ] If multiple candidates are eligible, one is promoted per scheduler cycle and the remainder stay queued
- [ ] Automatic paper promotion does not authorize authenticated real-money execution

## Shadow isolation

Required when `shadow-isolated` is used.

- [ ] Shadow outputs use separate files/state/telemetry
- [ ] Shadow code cannot emit production intents or authenticated orders
- [ ] Shadow estimates and hypothetical fills are not booked as realized PnL
- [ ] Production thresholds, sizing, exposure, drawdown, kill switch and OOS gates are unchanged
- [ ] Failure is visible but cannot corrupt the production decision path
- [ ] Deterministic tests enforce the isolation boundary

## Model and execution boundaries

- [ ] Paper fills are not presented as real fills
- [ ] No authenticated order submission, wallet secret or credential is introduced
- [ ] State persistence, restart behavior, reconciliation and kill-switch effects are considered
- [ ] Automatic paper promotion does not implicitly authorize real-money execution

## Scheduler and authority boundaries

- [ ] This change preserves one job and one bounded responsibility per workflow
- [ ] Only the integration scheduler can merge an automatic paper integration
- [ ] Only the post-merge scheduler can dispatch the validation bundle
- [ ] Only the deployment scheduler can deploy `paper-validated`
- [ ] The administrator supervisor remains read-only
- [ ] The meta-supervisor may dispatch the integration scheduler but cannot merge directly

## Branch lifecycle and post-merge verification

- [ ] Research is on `research/*`, `experiment/*` or `diagnostic/*`, not directly on `main`
- [ ] A live candidate is consolidated on a fresh `integration/*` branch based on current `main`
- [ ] The integration PR links its numbered source evidence and can be squash-merged as one coherent champion change
- [ ] Integration merge and post-merge validation are handled by separate schedulers
- [ ] CI, monitoring and live-paper smoke are bound to the exact merged SHA
- [ ] Promotion is deployment-complete only after `main == paper-validated == deployed HEAD`
- [ ] The head branch can be deleted after merge or closure
