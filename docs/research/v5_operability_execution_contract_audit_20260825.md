# V5 model operability and execution-contract audit

Date: 2026-08-25  
Base revision: `810c98da0661826bc19aae032b9893e867f238c7`  
Scope: live-data paper trading only; no authenticated execution.

## Question

Why do several V5 models appear alive but either do not trade or trade through an economically invalid path?

## Evidence inspected

1. `telemetry/latest-live-smoke.json` at the base revision.
2. Generic expert construction, ensemble, sizing, entry and exit logic in `src/engine.cpp`.
3. CLI semantics in `src/main.cpp`.
4. Independent-strategy orchestration in `scripts/multi_strategy_paper.py` and `scripts/paper_v5_loop.sh`.
5. Passive maker execution in `src/maker_paper.cpp`.
6. B1/B2 intent construction and multi-leg paper execution in `scripts/build_v4_intents.py` and `src/multileg_paper.cpp`.
7. Structural NegRisk diagnostics in `src/negrisk_arb.cpp`.
8. Current external feed state in `data/external_signals.csv`.

## Observed runtime state

The latest validated sample does not support the interpretation that all inactive models are dead:

- market/trade recording is fresh and broad;
- B1 has a raw-positive candidate, but its best executable maker edge is negative after costs;
- B2 has coherent raw-positive candidates, but its best executable maker edge is slightly negative and no production intent is created;
- the passive maker has posted resting quotes, but there is no evidenced queue-consuming fill in the sampled interval;
- the generic five-child allocator retains nine live units and approximately `-16.92 USD` aggregate marked PnL;
- the configured external CSV contains no fresh positive-confidence signal rows;
- OOS promotion gates have no executed observations and therefore remain closed.

Thus at least four distinct states are currently conflated by the word “not operating”:

1. valid economic abstention after executable costs;
2. passive orders resting without fill evidence;
3. missing or unapproved model inputs;
4. an invalid model-to-execution mapping despite a live process.

## Execution-contract defects

### 1. Generic one-expert execution is not universal

Each V5 child is produced by assigning a one-hot expert weight and launching the same generic binary engine. The engine interprets the resulting quantity as a terminal YES probability, compares it with a single executable side, applies binary Kelly sizing, and opens a directional position.

That contract is not valid for all five experts:

- **Microstructure** estimates short-horizon price pressure. Its identified route is passive maker execution with queue and adverse-selection evidence, not generic taker entry sized as a terminal event bet.
- **PCA/stat-arb** estimates a short-horizon relative-value markout. Its identified route is a coherent hedged B2 bundle, not a naked binary leg held under terminal-probability semantics.
- **Graph constraints** identify inequalities or complete-event basket inconsistencies. A sum constraint alone does not identify which individual leg is directionally wrong.
- **Semantic similarity** does not identify event equivalence, polarity, threshold direction, horizon, or conditional relation. Token overlap is therefore not a terminal-probability estimator.
- **External information** can satisfy the terminal-probability contract only when a fresh, mapped, calibrated, approved feed exists. The current file has no such rows.

### 2. Process liveness is insufficient

The shell supervisor restarts a child only after process exit. A process can remain alive while blocked in a network call or otherwise stop publishing fresh state. The allocator already computes per-model status age, but no watchdog acts on it.

### 3. Operability telemetry is semantically incomplete

Existing metrics expose PID liveness, PnL, positions and recent generic signals. They do not identify the actual executable backend or distinguish:

- active fills/positions;
- admitted orders waiting in queue;
- negative post-cost abstention;
- missing input;
- research-only shadow state;
- stale process state.

### 4. Portfolio accounting is fragmented

The generic allocator, passive maker and multi-leg broker maintain separate paper accounts and risk state. This audit does not claim that cross-backend capital, exposure and drawdown are already jointly reconciled. Until a single broker/portfolio ledger exists, executable backends must remain paper-only and their aggregate exposure must be monitored explicitly.

## Minimum safe operational correction

The evidence supports an operational containment change, not an alpha promotion:

1. keep generic children running for diagnostics, exit management and settlement;
2. force generic children to scan-only so they cannot open new misrouted positions;
3. retain new-entry capability only in the identified paper backends:
   - microstructure → passive maker;
   - PCA → coherent B2 multi-leg broker;
   - pair stat-arb → B1 multi-leg broker;
4. keep graph, semantic and external new entries fail-closed;
5. add a status-age watchdog that restarts the allocator through the existing supervisor;
6. publish an execution-aware per-model operability matrix and Prometheus metrics;
7. do not relax executable-edge, fill-evidence, OOS, drawdown or real-money gates.

## Verification requirements

The integration is acceptable only if all of the following hold:

- unit tests prove scan-only is always injected into generic children;
- engine CLI semantics confirm scan-only prevents new entries while the normal loop continues exit/resolution processing;
- tests distinguish stale children from valid abstention and queue waiting;
- monitoring exports backend and state without high-cardinality reason labels;
- the live champion remains paper-only;
- maker and B1/B2 cost gates remain unchanged;
- graph, semantic and external cannot open new positions;
- no authenticated order path or secret is added.

## Explicit non-approval

This audit does not approve:

- lowering thresholds merely to manufacture activity;
- treating raw edge as executable edge;
- synthetic or unevidenced fills;
- naked PCA execution;
- single-leg graph execution from a probability-sum constraint;
- semantic execution without relation and polarity identification;
- external execution without a fresh approved probability feed;
- real-money order submission.
