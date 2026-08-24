## What changes

Describe the implementation and the economic/statistical object it estimates or controls.

## Lifecycle status

Select exactly one primary status.

- [ ] Normal feature, bug fix, execution/risk, documentation or infrastructure change
- [ ] Research/experiment/diagnostic only — evidence source; close without direct merge
- [ ] Shadow-only instrumentation — requires `shadow-isolated`
- [ ] Approved research integration into the single champion — requires an `integration/*` branch plus research and administrator approval

## Research evidence

Required for research, shadow and integration work.

- Source research PR/branch/commit:
- Hypothesis or measurement question:
- Current champion baseline:
- Candidate specification:
- Common chronological sample / information set:
- Executable-cost assumptions:
- Normal and stressed result:
- Decision: `REJECTED` / `MORE_EVIDENCE_REQUIRED` / `APPROVED_FOR_INTEGRATION` / `SHADOW_ONLY`

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

Required for every approved integration.

- [ ] The candidate is connected to the existing expert/signal/intent interfaces rather than left as a second complete live stack
- [ ] There remains one model orchestrator/registry, one live config, one portfolio/risk allocator and one execution broker
- [ ] Terminal probabilities are not confused with mark-to-market relative-value signals
- [ ] Structural, external-information and execution estimates retain their correct economic meaning
- [ ] Duplicated or superseded implementation, configuration, state and telemetry paths are removed or given a documented deletion condition
- [ ] Incremental alpha is shown with an ablation against the incumbent champion
- [ ] `config/live_champion.json` is unchanged, or its promotion change is explicit, reviewed and rollback-safe
- [ ] The previous `paper-validated` revision remains the rollback target until post-merge live smoke succeeds

## Research and administrator approval

Required only for a non-draft `integration/*` PR.

- [ ] The PR links `Source research PR/branch/commit: #<number>`
- [ ] The source research PR carries `research-approved` and has green Release, Debug and research-policy checks
- [ ] The integration PR carries `approved-for-integration`, `single-model-reviewed` and `administrator-approved`
- [ ] The integration PR has green Release, Debug, monitoring, research-policy and live-paper checks
- [ ] The project administrator reviewed this exact integrated diff, not only the source research result
- [ ] Research or administrator approval does not authorize authenticated real-money execution

## Shadow isolation

Required when `shadow-isolated` is used.

- [ ] Shadow outputs use separate files/state/telemetry
- [ ] Shadow code cannot emit production intents or authenticated orders
- [ ] Shadow estimates and hypothetical fills are not booked as realized PnL
- [ ] Production thresholds, sizing, exposure, drawdown, kill switch and OOS gates are unchanged
- [ ] Failure is visible but cannot corrupt or block the production decision path
- [ ] Deterministic tests enforce the isolation boundary

## Model and execution boundaries

- [ ] Paper fills are not presented as real fills
- [ ] No authenticated order submission, wallet secret or credential is introduced
- [ ] State persistence, restart behavior, reconciliation and kill-switch effects are considered
- [ ] Model approval and administrator approval do not implicitly authorize real-money execution

## Scheduler and authority boundaries

- [ ] This change preserves one job and one bounded responsibility per workflow
- [ ] Only the integration scheduler can merge an approved integration
- [ ] Only the post-merge scheduler can dispatch the validation bundle
- [ ] Only the deployment scheduler can deploy `paper-validated`
- [ ] The administrator supervisor remains read-only
- [ ] The meta-supervisor may dispatch the integration scheduler but cannot provide approvals or merge directly

## Branch lifecycle and post-merge verification

- [ ] Unapproved research is on `research/*`, `experiment/*` or `diagnostic/*`, not directly on `main`
- [ ] Approved research is consolidated on a fresh `integration/*` branch based on current `main`
- [ ] The integration PR links its numbered source evidence and can be squash-merged as one coherent champion change
- [ ] Integration merge and post-merge validation are handled by separate schedulers
- [ ] CI, monitoring and live-paper smoke are bound to the exact merged SHA
- [ ] Promotion is complete only after `main == paper-validated == deployed HEAD`
- [ ] The head branch can be deleted after merge or closure
