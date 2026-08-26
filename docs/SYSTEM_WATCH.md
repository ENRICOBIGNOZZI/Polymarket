# Polymarket V7 System Watch

System Watch is the V7 control plane. The authoritative scheduler registry is `config/scheduler_registry.json`; current operator authority is `config/operator_directives.json`.

## Invariants

The operational system has exactly one PAPER runtime:

```text
main -> exact validation -> paper-validated -> deployed V7
```

`config/live_champion.json` must select:

```text
version=7
loop=scripts/paper_v7_loop.sh
config=config/paper_v7.json
run_root=runs/paper_v7_live
```

Retired predecessor runtimes, multi-version selectors, compatibility adapters and duplicate execution owners are forbidden.

The states remain distinct:

```text
merged != validated != deployed != healthy
```

No scheduler may collapse those transitions by assumption.

## Authority split

Only the registered workflows may act, and authority is intentionally narrow:

- **Administrator Supervisor** — observes control-plane state and blockers.
- **Research Policy** — enforces branch/provenance/operator-directive/shadow-isolation rules.
- **Research Director** — allocates bounded research work.
- **Promotion Controller** — decides whether an integration is objectively promotable from current evidence.
- **Integration Merge** — sole merge authority; merges only a controller-authorized integration after immediate revalidation.
- **Post-Merge Validation** — sole validation-dispatch authority; dispatches exact-SHA CI, monitoring and V7 live PAPER smoke.
- **CI** — deterministic code/V7-only repository validation.
- **Monitoring** — V7 observability/runtime-contract validation.
- **V7 live PAPER smoke** — exact-SHA public-data runtime validation and the only workflow that may advance `paper-validated` after success.
- **Paper Server Deploy** — sole deployment authority; deploys only `paper-validated`.
- **Paper Server Health** — read-only verification of deployed SHA, process ownership, execution state and monitoring.
- **Research workflows** — evidence collection only; no merge/deploy/champion/authenticated-execution authority.

Manual approval is not required for PAPER promotion. Automatic promotion remains subject to the current operator directives, exact provenance, technical gates, executable-cost evidence, statistical stability, data health and risk constraints.

## Branch policy

### `main`

Contains the single integrated V7 code line. It must not contain alternate complete runtimes or compatibility copies of retired versions.

### `research/*`, `experiment/*`, `diagnostic/*`

Contain unapproved hypotheses, measurements or fault isolation. They do not own production intents, booked PnL, sizing, risk or deployment.

### `integration/*`

The only class for consolidating an approved change into current V7. An integration branch starts from current `main`, carries exact source provenance, removes superseded implementation, and remains one V7 runtime after the change.

Focused `fix/*`, `feat/*`, `improve/*` and `ops/*` branches may change bounded implementation/infrastructure but cannot create a second live runtime.

## Research gate

A promotable change must satisfy, as applicable:

- causal/time-correct data and deterministic/replayable decisions;
- chronological train/calibration/test separation;
- no leakage or post-hoc frequency pooling;
- dependence/multiple-testing controls where relevant;
- executable prices, depth, fees, slippage, queue/fill, latency, adverse markout and capital-time economics;
- explicit partial-fill/unwind economics for multi-leg strategies;
- stable OOS evidence and incremental portfolio utility;
- data-health and state-integrity checks;
- 15% aggregate PAPER drawdown hard limit;
- PAPER-only/authenticated-execution-disabled separation.

Insufficient evidence means no promotion. Thresholds are not weakened merely to manufacture fills or PnL.

## Integration procedure

For an approved research result:

1. Start from current `main`.
2. Bind the integration to exact source evidence and source code.
3. Port only the reviewed reusable change into current V7.
4. Preserve one runtime owner, execution ledger, broker authority and risk state.
5. Delete superseded code/config/tests/workflows immediately; do not add a compatibility path for retired runtimes.
6. Re-run deterministic and integrated evidence.
7. Let the Promotion Controller issue an ephemeral authorization only if current gates pass.
8. Let Integration Merge merge exactly that authorized head after current-base/exact-head revalidation.
9. Dispatch CI, Monitoring and V7 live PAPER smoke for the exact merged SHA.
10. Advance `paper-validated` only after exact-SHA V7 smoke success.
11. Deploy only that `paper-validated` revision.
12. Require server health to prove the deployed V7 state.
13. Delete short-lived branches/scaffolding when they are no longer needed.

## Shadow research

Shadow research is allowed only when it is isolated from execution state and booked PnL. It may write its own files/metrics but cannot:

- emit production intents;
- submit authenticated orders;
- change sizing/exposure/drawdown/kill state;
- alter `config/live_champion.json`;
- block execution because a measurement-only artifact is missing.

## Reporting

Administrator Supervisor reports:

- current `main`, `paper-validated` and their relation;
- V7 loop/config/run root;
- latest state of registered schedulers;
- research/integration queue;
- exact validation/deployment blockers;
- any attempted reintroduction of retired runtime surfaces.

No-change runs must state why no transition was taken. Silence is not health evidence.
