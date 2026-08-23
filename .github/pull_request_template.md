## What changes

Describe the implementation and the economic/statistical object it estimates.

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
- [ ] Costs, spread, slippage, depth and uncertainty are handled at executable prices where relevant

## Model and execution boundaries

- [ ] Terminal probabilities are not confused with mark-to-market relative-value signals
- [ ] Paper fills are not presented as real fills
- [ ] No authenticated order submission, wallet secret or credential is introduced
- [ ] State persistence, restart behavior and kill-switch effects are considered

## Branch lifecycle

- [ ] Reusable code should be merged; temporary experiments should record their result and close without merge
- [ ] The head branch can be deleted after merge or closure
