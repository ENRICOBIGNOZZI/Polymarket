# Polymarket V7 — World-Class Implementation Program

## Purpose

This is the master implementation program for Codex and human review. Its target is not "more features". Its target is a V7 system whose measured information edge, execution quality, capital efficiency, reliability and learning rate are strong enough to be credible as a professional prediction-market trading platform.

No document can guarantee best-in-world status. This program therefore defines measurable conditions that would justify progressively stronger claims.

Always treat current `main`, current operator directives and current telemetry as authoritative over any stale snapshot in this document.

---

## 0. Non-negotiable architecture

Keep:

- V7 only;
- one runtime owner;
- one OMS;
- one canonical ledger/writer;
- one inventory truth;
- one allocator;
- one global risk/kill layer;
- one monitoring plane;
- one champion per decision surface with challengers isolated from promotion authority.

Do not create a new numerical generation or duplicate runtime to solve a local problem.

Current authority remains PAPER-only unless superseded by a later explicit operator directive.

---

## 1. North-star objective

The long-run decision problem should converge to:

```text
state X_t
  -> settlement-aware probability / fair-value distribution
  -> execution-state distribution conditional on action
  -> markout / inventory / unwind distribution conditional on fill
  -> robust action EV
  -> risk/capital constrained action
  -> canonical OMS
  -> outcome evidence
  -> learning factory
```

with action space:

```text
MAKE / TAKE / CANCEL / WITHDRAW / NOTHING
```

and with risk/liquidation actions pre-empting alpha actions.

A generic target representation is:

```text
EV(a|X)
 = P(fill or joint execution state | X,a)
   * E(alpha/spread/reward - fees - slippage - adverse markout | fill,X,a)
   - E(partial/inventory/unwind loss | X,a)
   - capital cost
   - latency cost
```

For multi-leg actions, use empirical joint states rather than products/minima of marginal fill probabilities.

---

# PROGRAM PHASES

## P0-A — Maker Profitability / Fillability / Action EV

### Goal

Turn the professional maker from a technically sophisticated quoting engine into a causally learned action policy that knows where, when, how and for how long to quote — and when to withdraw.

### Required state/action features

At minimum preserve and validate:

- exact market/event identity;
- receive-time book state;
- bid/ask/spread;
- L1/L3/L5 depth and imbalance;
- microprice;
- OFI / signed public flow across multiple windows;
- aggressive flow freshness and reachability;
- queue-ahead lower/expected/upper and confidence;
- quote placement/action identity;
- quote age/dwell;
- cancellation reason and effective cancel timing;
- short volatility / jump regime;
- TTE/TTR;
- inventory sign/age/gross exposure;
- reward/rebate context with provenance;
- fair-value disagreement when available;
- end-to-end latency features;
- exact model/policy/SHA identity.

### Required labels

For every order/decision cohort where causally observable:

- aggressive-flow reached quote before horizon;
- first-fill time;
- fill fraction / partial-fill trajectory;
- cancel-before-fill / fill-during-cancel-pending;
- markout at 1s/10s/45s/60s/300s or relevant horizons;
- inventory duration;
- forced/unforced unwind cost;
- terminal/settlement PnL where applicable;
- maker rebate;
- liquidity reward;
- zero-reward counterfactual;
- total realized economic PnL.

### Models

Maintain simple interpretable baselines first, then challengers:

1. empirical bucketed fill hazard;
2. beta-binomial/logistic partial-pooling fill model;
3. survival/hazard challenger;
4. fill-conditioned markout regression;
5. distributional markout challenger;
6. inventory/unwind model;
7. action-conditional robust EV layer.

Do not replace transparent baselines merely because a complex learner fits better in-sample.

### Policy

For each admissible action:

```text
JOIN / IMPROVE1 / FADE1 / FADE2 / ONE_SIDED / WITHDRAW
```

estimate robust EV and choose the maximum positive action after risk constraints. `WITHDRAW` or `NOTHING` must be normal optimal outcomes.

### Exploration

Keep bounded propensity-logged exploration so that policy changes do not destroy identifiability. Exploration:

- never bypasses hard safety;
- never crosses if post-only is required;
- has strict capital/market/order caps;
- logs action propensity and eligibility set;
- receives no automatic promotion credit;
- is evaluated separately from exploitation.

### Acceptance criteria

Implementation-complete requires the complete causal lifecycle linkage and learner/policy surfaces to exist and pass deterministic tests.

Economic-complete requires forward evidence showing, on independent event/order clusters and same-sample cost stress:

- materially non-zero fill population;
- stable fill-hazard calibration;
- fill-conditioned markout not systematically destroying spread capture in promoted cells;
- positive robust net EV/PnL in promoted cells;
- negative/toxic cells suppressed by policy;
- improvement versus incumbent/common-support baseline;
- no dependence on unverified rewards;
- no causal failures.

If not enough observations exist, state `MORE_EVIDENCE_REQUIRED` and continue collection.

---

## P0-B — Continuous Learning & Experimentation Factory

### Goal

Make every new causal observation capable of improving V7 through a reproducible champion/challenger process without automatic promotion or leakage.

Canonical loop:

```text
raw immutable events
 -> provenance validation
 -> point-in-time features
 -> causal labels
 -> immutable dataset manifest
 -> train
 -> challenger artifact
 -> chronological OOS
 -> deterministic replay
 -> forward shadow/PAPER
 -> champion/common-support comparison
 -> promotion packet
 -> explicit operator decision
```

Detailed contract: `docs/CONTINUOUS_LEARNING_FACTORY.md`.

### Acceptance criteria

- dataset manifests are immutable/versioned;
- feature/label timing is mechanically validated;
- no random shuffle for time-dependent evidence;
- challengers can be trained without mutating champion;
- refit does not imply promotion;
- same-observation 1x/1.5x/2x stress is available;
- promotion packets are machine-readable and human-readable;
- rollback identity is exact;
- scheduled data collection/refit/evaluation may run unattended;
- promotion remains gated.

---

## P0-C — Settlement-Aware External Fair Value

### Goal

Demonstrate an independent, settlement-correct probability/fair-value edge that improves decisions relative to Polymarket and the incumbent.

### Required components

Preserve/complete:

- exact contract/rules hash and outcome mapping;
- exact resolution-source/oracle semantics;
- same-oracle binding where required;
- external venues with health, basis and disagreement metrics;
- receive-time causality;
- deterministic replay;
- structural settlement probability;
- oracle bridge;
- pure-external benchmark;
- hybrid benchmark separated from pure external;
- uncertainty decomposition;
- calibration by TTE/regime;
- fair-value snapshot with lower/upper bounds;
- champion/challenger registry.

### Required tests

- resolved-contract probability scoring: Brier/log loss/ECE/calibration slope;
- incremental predictive value over PM price baseline;
- incremental economic value over incumbent on common observations;
- robust executable replay PnL with exact fees/depth/latency assumptions;
- 1x/1.5x/2x cost stress;
- drift/regime stability;
- zero causality violations;
- exact rules scope match.

### Promotion sequence

```text
SHADOW
 -> frozen OOS evidence
 -> frozen forward shadow
 -> PAPER counterfactual actions
 -> bounded PAPER repricing/cancel influence
 -> bounded PAPER taker influence
 -> broader PAPER only after evidence
```

Do not enable economic authority merely because code exists.

---

## P0-D — Unified MAKE / TAKE / CANCEL / WITHDRAW Policy

### Goal

Stop treating maker and informed taker as unrelated economic engines when they act on the same contract state.

### Required logic

At each causal state, produce comparable robust values for:

- passive make at candidate placements;
- aggressive take at executable depth;
- cancel/withdraw current exposure;
- inventory reduction;
- do nothing.

Respect priority:

```text
KILL
DANGEROUS_CANCEL
LIQUIDATION
INVENTORY_REDUCTION
POSITIVE_ROBUST_TAKE
POSITIVE_ROBUST_MAKE
NOTHING
```

### Critical constraints

- no self-cross;
- canonical cancel-confirm state before conflicting take;
- exact fees and depth;
- taker delay/race effects where applicable;
- fair-value uncertainty separate from execution uncertainty;
- inventory and capital shared across actions;
- same ledger attribution.

### Acceptance criteria

A single decision packet can explain why MAKE, TAKE, CANCEL, WITHDRAW or NOTHING won and reconstruct every cost/uncertainty component.

---

## P0-E — End-to-End Latency Domination

### Goal

Measure and optimize the latency that actually determines opportunity capture rather than synthetic internal microbenchmarks.

### Required stage clocks

Measure at least:

```text
source/exchange timestamp diagnostic
local socket receive
parse complete
book state committed
features/fair complete
decision complete
risk complete
OMS enqueue
serialization/sign complete
packet/request sent
venue ack
cancel ack
private/user-stream confirmation
```

Report p50/p90/p95/p99/p99.9/max, reconnect health, packet loss/gaps and opportunity lifetime.

### Regional shootout

Run same binary/config/SHA across candidate regions. Choose using stable tail latency and health, not median alone.

Do not claim top-percentile execution without comparable live evidence.

### Optimization ordering

Fix the largest measured component first. Do not spend major effort on sub-100us internal stages if multi-ms network/venue delay dominates.

### Acceptance criteria

- exact-head representative live-PAPER timing exists;
- stage decomposition sums coherently;
- regional winner is evidence-based;
- cancel effectiveness is measured against toxicity/adverse events;
- performance regressions are CI/monitor-visible.

---

## P0-F — Canonical Execution / Risk / Reconciliation Hardening

### Goal

Make state correctness survive failures, reconnects and partial execution.

Cover:

- single-writer enforcement;
- idempotent intent/order identity;
- decision -> OMS -> ack -> private-stream lifecycle join;
- unknown state reconciliation;
- cancel-pending fillability;
- reconnect lineage invalidation;
- restart/recovery without fabricated fills/PnL;
- inventory terminalization rules;
- kill propagation;
- exact capital reconciliation;
- fail-closed fee/contract/market-state behavior.

Acceptance requires deterministic fault-injection tests and forward operational evidence.

---

## P1-A — Structural / Hard / Graph Arbitrage Excellence

### Goal

Validate structural edges only after executable joint-state economics.

Require:

- full visible depth;
- sequential leg revalidation;
- empirical joint completion distribution;
- explicit partial-state inventory;
- forced-completion/unwind paths;
- fee/latency/cancel race modeling;
- common-sample cost stress;
- opportunity capacity curve;
- realized/forward evidence separated by family and horizon.

No static locked edge alone may authorize an alpha claim.

---

## P1-B — Capacity-Aware Portfolio Allocator

### Goal

Replace arbitrary equal sleeve budgets with evidence-weighted marginal capital allocation once sufficient evidence exists.

Estimate for each sleeve/regime:

- expected post-cost return;
- uncertainty;
- drawdown/tail risk;
- correlation with other sleeves;
- executable capacity;
- marginal degradation with size;
- inventory liquidity/unwind time;
- latency sensitivity;
- subsidy dependence.

Optimize under global cash/gross/market/event/drawdown/kill constraints.

Acceptance requires conservative behavior under sparse evidence and explicit capacity saturation.

---

## P1-C — OSINT / Market-Open Alpha

### Goal

Turn research kernels into verified incremental information sources only where semantic mapping, causal timing and executable economics pass.

Require:

- source hierarchy with official/primary-source preference;
- receive timestamps;
- exact event-to-contract mapping;
- correction/retraction handling;
- independent information-event clustering;
- edge-decay estimation;
- PM baseline and incumbent ablation;
- executable replay;
- frozen forward validation.

No generic LLM/news sentiment may receive execution authority without this chain.

---

## P2-A — Sports Latency

Proceed only after a verified low-latency streaming feed and independent game-to-contract mapping exist.

Model state transitions, market reaction delay, execution race, stale-state cancel logic and settlement mapping. Treat feed and mapping uncertainty as hard blockers.

---

## P2-B — Cross-Platform

Proceed only after a second executable venue adapter and semantic equivalence layer exist.

Require exact outcome/settlement equivalence, fees, capital movement/inventory constraints, cross-venue race, partial execution, and failure/recovery paths.

A correlated price is not an arbitrage relation.

---

## P2-C — Slower Ranking / PCA / Local Factor

Keep horizons separate. Require incremental common-sample value after realistic execution and cost stress. Do not pool evidence across model families/horizons merely to obtain sample size.

---

# CROSS-CUTTING REQUIREMENTS

## Data quality

- immutable raw event store;
- exact receive-time provenance;
- explicit missingness;
- schema versioning;
- reproducible derived data;
- no silent repairs;
- resolved-outcome backfill cannot alter historical point-in-time features.

## Statistical discipline

- chronological walk-forward;
- purge/embargo where labels overlap;
- event/market clustering;
- confidence intervals / bootstrap appropriate to dependence;
- fold stability;
- challenger vs incumbent on common support;
- multiple-testing awareness for research factory;
- capacity and regime stratification;
- frozen final evaluation sets.

## Economics

Always separate:

```text
trading PnL
maker rebates
liquidity rewards
total economic PnL
```

and test zero-reward/rebate-stressed counterfactuals.

## Observability

Every major mission must expose enough Prometheus/Grafana/structured status evidence to answer:

- what is running;
- exact SHA/config/model;
- what is blocked and why;
- how many independent observations exist;
- fill/markout/PnL by action/regime;
- latency tails;
- data freshness/causality;
- champion/challenger state;
- kill/risk/reconciliation state.

## Repository quality

- no duplicate runtime surfaces;
- no stale operational generations;
- CI must enforce V7 shape;
- add branch/ruleset governance when possible;
- keep secrets out of repository/history;
- docs must describe actual implementation, not aspirations.

---

# DEFINITION OF DONE FOR THE PROGRAM

The full program is not done when every issue is coded. It is done only when the system has accumulated sufficient independent forward evidence to support all claims made about it.

A credible professional/world-class candidate should eventually demonstrate:

1. positive forward maker economics in promoted state/action cells;
2. independent external information value with calibrated probabilities;
3. positive unified make/take economics after realistic costs;
4. competitive and stable end-to-end tail latency on the selected region;
5. structural/relative-value opportunities surviving joint execution and unwind costs;
6. allocator behavior consistent with capacity and cross-sleeve risk;
7. reproducible continuous-learning loop that improves challengers without leakage;
8. robust performance under 2x cost stress and reduced/zero subsidies where relevant;
9. stable behavior across multiple regimes/time windows;
10. operational correctness through reconnects, partial fills, restarts and state reconciliation.

Until those exist, report the strongest narrower statement supported by evidence.
