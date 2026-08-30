# Polymarket V7 — Continuous Learning & Experimentation Factory

## Objective

Build a causal, reproducible, continuously improving research-to-production loop in which new observations can create better challengers without contaminating the incumbent or silently changing execution authority.

Codex is responsible for building, testing and improving this factory. Statistical learners trained from V7 data are responsible for learning the policies/models.

The factory must optimize real executable economics, not backtest fit.

---

## 1. Canonical lifecycle

```text
1. RAW EVENT CAPTURE
2. IMMUTABLE PROVENANCE
3. POINT-IN-TIME FEATURE BUILD
4. CAUSAL LABEL BUILD
5. DATASET MANIFEST / FREEZE
6. TRAIN BASELINE + CHALLENGER
7. CHRONOLOGICAL VALIDATION
8. FROZEN OOS / REPLAY
9. FORWARD SHADOW/PAPER
10. CHAMPION COMMON-SUPPORT ABLA/COMPARISON
11. PROMOTION PACKET
12. EXPLICIT PROMOTION OR REJECTION
13. CONTINUE DATA COLLECTION
```

No stage may silently skip the previous stage's identity/provenance requirements.

---

## 2. Raw event capture

Preserve immutable receive-time events needed to reconstruct decisions and outcomes.

At minimum, depending on sleeve:

- Polymarket public book deltas/snapshots;
- public trades with side/sign inference provenance;
- market metadata/rules/fees/reward state;
- external venue updates;
- settlement-oracle observations;
- contract/rules binding status;
- OSINT/source events;
- decision inputs;
- strategy intents;
- risk decisions;
- OMS submissions;
- venue acknowledgements;
- private/user stream order state;
- cancel lifecycle;
- fills/partial fills;
- inventory/account state;
- settlement/resolution labels.

Required clocks:

- source/exchange timestamp when supplied, diagnostic unless explicitly authoritative;
- local receive monotonic timestamp;
- local wall clock for human observability;
- decision monotonic timestamp;
- send/ack/cancel/private-stream timestamps as applicable.

Do not overwrite raw evidence. Repairs or normalization must create derived records with provenance.

---

## 3. Event identity and lifecycle joins

Every executable observation must be reconstructable through stable identities.

Required identities where applicable:

- market_id / condition_id / token_id;
- event_id / semantic relation id;
- decision_id;
- intent_id;
- order client id;
- venue order id;
- cancel id;
- fill/trade id;
- model/policy version;
- code SHA;
- runtime/session id;
- universe/cohort id;
- exploration arm/propensity id.

Lifecycle joins must survive asynchronous decision -> OMS -> ack -> private-stream timing and restarts.

Missing joins must be explicit data-quality states, not dropped silently.

---

## 4. Point-in-time feature contract

A feature is valid for a decision only if its authoritative receive time is no later than the decision cut.

Mechanical invariant:

```text
max(authoritative_input_receive_monotonic_ns) <= decision_monotonic_ns
```

Any feature depending on future information must fail the causal audit.

Feature rows must store:

- feature schema/version/hash;
- exact source records or reproducible offsets/pointers;
- observation cut time;
- missingness/health flags;
- transformation version;
- model-family-specific identity.

Do not let resolution backfills or later metadata rewrite historical features.

---

## 5. Label contract

Labels must be generated from outcomes strictly after the causal feature cut and must preserve exact horizon semantics.

### Maker labels

For each eligible decision/order/action:

- quote became live;
- time to first aggressive flow that could reach price;
- flow reached estimated queue lower/expected/upper;
- time to any fill;
- time to fill fraction thresholds;
- partial/full fill state;
- cancel requested/effective state;
- fill during cancel-pending;
- markout at configured horizons using executable/authoritative marks;
- realized inventory/unwind cost;
- inventory duration;
- terminal/settlement result if relevant;
- trading PnL;
- rebate PnL;
- reward PnL;
- zero-subsidy counterfactual.

### Taker labels

- executable entry fraction;
- race/depth survival;
- fees/slippage;
- subsequent markout or hold-to-settlement PnL;
- opportunity decay;
- latency sensitivity;
- self-cross/cancel prerequisite state.

### Fair-value labels

- resolved binary outcome;
- TTE bucket;
- settlement-oracle target/bridge labels;
- future PM change for short-horizon ablation only where causally defined;
- exact resolution provenance.

### Multi-leg labels

- direct joint execution state;
- leg ordering;
- partial state;
- forced completion/unwind;
- terminal joint PnL.

Never replace direct joint-state labels with products of marginal probabilities.

---

## 6. Dataset manifest

Every training/evaluation dataset must have an immutable machine-readable manifest containing at least:

```text
dataset_id
created_at
source_session_ids
source_sha(s)
feature_schema_hash
label_schema_hash
strategy/family
market/contract scope
start/end receive time
training/validation/OOS cuts
purge/embargo policy
cluster definition
fee provenance
reward provenance
cost model version
row counts
independent cluster counts
missingness/data-quality summary
causality audit result
content hash / manifest hash
```

A model artifact must reference one exact manifest.

If the underlying data changes, create a new dataset id rather than mutating the old one.

---

## 7. Training hierarchy

For every model family, maintain simple baselines and challengers.

### Maker execution

Baseline ladder:

1. empirical action/queue/flow buckets;
2. beta-binomial / logistic partial pooling;
3. survival/hazard model;
4. hierarchical survival challenger;
5. fill-conditioned markout regression;
6. distributional markout challenger;
7. explicit inventory/unwind model;
8. robust action-EV policy.

### External fair value

Baseline ladder:

1. PM mid diagnostic only;
2. oracle-only structural;
3. external median;
4. structural + oracle bridge;
5. pure external learned model;
6. bounded microstructure challenger;
7. incumbent/challenger combination only if incremental value is proven.

### General rules

- prefer calibration and robustness over in-sample score;
- complexity must earn incremental OOS value;
- no random shuffle for time-dependent evidence;
- fit only on data preceding validation/OOS cuts;
- hyperparameter search must not consume final frozen test evidence;
- preserve training determinism where feasible;
- record random seeds and library/toolchain versions when stochastic components exist.

---

## 8. Chronological validation

Use expanding or rolling walk-forward consistent with the strategy horizon.

Required safeguards:

- purge overlapping labels where necessary;
- embargo around train/eval boundaries where information can leak;
- cluster by independent event/market/time unit appropriate to the strategy;
- report number of independent clusters, not only rows;
- preserve horizon/model-family separation;
- never pool unrelated horizons merely to pass a sample-size gate.

Metrics must include predictive, execution and economic metrics appropriate to the model.

---

## 9. Common-support incumbent comparison

Absolute challenger performance is insufficient.

Where possible construct a common-support set of states/opportunities where both incumbent and challenger can be scored.

Report at minimum:

```text
challenger metric
incumbent metric
incremental difference
confidence interval / bootstrap inference
fold stability
regime breakdown
capacity breakdown
```

For policies with differing action choices, retain off-policy evaluation only when action propensities and support are valid. Direct forward randomized/controlled evidence remains preferred for high-impact policy changes.

---

## 10. Exploration and off-policy evaluation

Bounded exploration is required to prevent the champion from censoring future learning.

Every exploratory decision must log:

- eligible actions;
- chosen action;
- selection probability/propensity;
- exploration policy version;
- reason for eligibility;
- hard-safety checks;
- state features at choice time.

Build support for:

- inverse propensity scoring diagnostics;
- stabilized/clipped IPS;
- doubly robust evaluation where appropriate;
- effective sample size;
- overlap/support diagnostics.

Do not use off-policy estimators outside valid support.

Exploration data must be reported separately from exploitation and cannot silently promote itself.

---

## 11. Cost and subsidy stress

Every executable economic evaluation must support same-observation stress.

At minimum:

```text
cost multiplier: 1.0x / 1.5x / 2.0x
reward multiplier: 1.0x / 0.5x / 0.0x
rebate multiplier: 1.0x / 0.5x / 0.0x where relevant
queue uncertainty: lower / expected / upper or equivalent
latency stress: strategy-appropriate perturbation
```

Do not reselect trades/observations separately for each stress multiplier unless explicitly presenting a different policy experiment. The canonical robustness test stresses the same frozen observations.

---

## 12. Capacity learning

For every economically promising sleeve, estimate performance as a function of size.

Track:

- available executable depth;
- queue-ahead impact from own size;
- fill probability vs size;
- slippage vs size;
- inventory duration vs size;
- forced-unwind loss vs size;
- opportunity count vs deployed capital;
- marginal expected PnL per added dollar;
- marginal risk/drawdown.

Never assume linear scaling from small PAPER sizes.

---

## 13. Challenger artifact

Every challenger must have an immutable artifact containing:

```text
candidate_id
family
model/policy type
code_sha
dataset_id
feature_schema_hash
training_window
training_end_time
hyperparameters
calibration object
policy thresholds
expected input schema
OOS metrics
stress metrics
forward status
promotion eligibility
reason codes
```

Publishing a challenger artifact does not change the champion pointer.

---

## 14. Forward shadow/PAPER stage

A challenger must collect independent forward evidence without using future outcomes in its decision state.

Forward records must bind exact:

- challenger id;
- incumbent id;
- state/decision id;
- counterfactual action if shadow;
- realized action if PAPER experiment;
- fill/markout/PnL outcome;
- regime and capacity state.

Freeze the challenger logic during a forward evaluation window. If the model is changed, create a new challenger id/window.

---

## 15. Promotion packet

The factory must generate a concise machine-readable and human-readable promotion packet.

Required sections:

1. identity and exact SHA;
2. hypothesis;
3. dataset/causality audit;
4. independent sample counts;
5. OOS metrics;
6. forward metrics;
7. incumbent/common-support delta;
8. 1x/1.5x/2x stress;
9. reward/rebate stress;
10. regime/fold stability;
11. capacity diagnostics;
12. known failure modes;
13. rollback artifact;
14. recommendation: `PROMOTE`, `REJECT`, or `MORE_EVIDENCE_REQUIRED`.

Automatic promotion is forbidden unless a later explicit operator directive changes that rule.

---

## 16. Drift and degradation monitoring

After promotion, continue comparing live/PAPER observations to training/OOS distributions.

Monitor:

- feature drift;
- calibration drift;
- fill-hazard drift;
- markout drift;
- PnL drift;
- regime mix;
- queue/flow dynamics;
- latency drift;
- venue/API/fee/reward changes;
- capacity saturation.

Drift may create a challenger/refit or withdraw authority depending on severity. It must not silently mutate champion parameters.

---

## 17. Scheduled automation

The repository should support unattended recurring jobs for:

- raw data capture;
- data-quality/causality audit;
- label maturation;
- dataset manifests;
- challenger training when minimum new evidence is reached;
- OOS/replay evaluation;
- drift reports;
- forward evidence summaries;
- stale champion/challenger detection;
- issue/report generation for material degradations.

Recurring jobs may train/evaluate challengers. They may not silently promote execution authority.

---

## 18. Data-quality failure modes

Explicitly classify and fail/skip appropriately for:

- out-of-order receive times;
- duplicated events;
- missing sequence/gap;
- reconnect lineage invalidation;
- stale book;
- missing order acknowledgement;
- unjoined private/public lifecycle;
- uncertain trade sign;
- unknown fee/reward state;
- invalid contract/rules mapping;
- missing/late resolution label;
- future source timestamp;
- clock anomaly;
- inconsistent inventory/account state.

Report dropped/invalid observations by reason. Never silently coerce them into valid rows.

---

## 19. Research experiment registry

Maintain an append-only or immutable experiment registry with:

```text
experiment_id
hypothesis
owner/agent
base_sha
candidate_sha
dataset_id
model/policy ids
start/end
status
primary metric
secondary metrics
stress results
forward result
decision
links to artifacts/PR/issues
```

This registry is the institutional memory of why the champion changed.

---

## 20. Codex operating behavior

When Codex receives new evidence, it should not immediately add features.

Required sequence:

1. reproduce the evidence;
2. locate the economic failure mode;
3. verify data quality/causality;
4. formulate a testable hypothesis;
5. identify the smallest experiment/change;
6. predefine acceptance/rejection criteria;
7. implement challenger/instrumentation;
8. run deterministic tests and causal replay;
9. collect forward evidence when required;
10. update experiment registry;
11. recommend promote/reject/more-evidence.

Feature additions without an identified bottleneck and evaluation plan should normally be rejected.

---

## 21. Initial factory milestones

### Milestone 1 — lifecycle-complete maker dataset

Prove that quote decisions can be joined through OMS/ack/fill/cancel/markout/PnL with exact identity and causal timing.

### Milestone 2 — maker action-conditioned learner

Produce calibrated fill/survival and markout estimates by action/state with stable OOS diagnostics.

### Milestone 3 — challenger policy generator

Generate a policy challenger from robust EV estimates without mutating champion.

### Milestone 4 — automated OOS + stress packet

Produce same-sample cost/reward/queue stress and incumbent comparison.

### Milestone 5 — forward experiment manager

Run bounded propensity-logged PAPER experiments and freeze candidate identities during evaluation.

### Milestone 6 — external-fair integration

Use the same factory for settlement-aware probability challengers and unified make/take policy candidates.

### Milestone 7 — allocator/capacity learning

Feed validated sleeve-level marginal EV/capacity/risk estimates into the canonical allocator.

---

## Definition of done

The learning factory is implementation-complete when a new immutable batch of causal observations can automatically create a fully versioned challenger and complete the evaluation pipeline through a promotion packet without manual data munging.

It is economically useful when repeated challenger cycles demonstrate incremental forward improvements versus the incumbent without leakage, hidden sample changes or uncontrolled risk.
