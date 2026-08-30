# AGENTS.md — Polymarket V7 World-Class Engineering Contract

This file is the persistent operating contract for every Codex/agent task in this repository.

## 1. Mission

Maximize the probability that the canonical Polymarket V7 system becomes a world-class prediction-market trading system through measurable information edge, execution quality, capital efficiency, reliability, and learning speed.

"World-class" is a target, not a permitted claim. Never claim that the system is best-in-world, top-percentile, profitable, production-ready, or economically mature without direct evidence supporting that exact statement.

The objective is not to maximize code volume, strategy count, architectural novelty, or benchmark vanity metrics. The objective is robust forward post-cost economic performance under causal, reproducible, auditable execution evidence.

## 2. Permanent architecture invariants

Before editing anything, read current `main`, `config/operator_directives.json`, `README.md`, the relevant V7 configs, recent commits/PRs, and existing tests. Do not reimplement work already present.

Mandatory invariants:

- V7 is the only supported numerical runtime generation.
- Do not create V8/V9/V10 or another trading runtime.
- One runtime owner.
- One OMS / execution authority.
- One canonical append-only execution ledger and writer.
- One inventory truth.
- One account allocator.
- One global risk/kill layer.
- One canonical monitoring plane.
- Professional maker, informed taker, structural arb, graph/RV, research and slower models are sleeves/capabilities of V7, never duplicate runtimes.
- Preserve only generation-neutral/common primitives actually used by V7.
- Do not restore V3/V4/V5/V6 compatibility wrappers or operational surfaces.
- Git history is the archive for retired generations.

## 3. Safety and authority

Current repository authority is PAPER-only unless a later explicit operator directive says otherwise.

Do not add or enable:

- authenticated execution;
- wallet/key handling in the canonical runtime;
- real order submission;
- real capital at risk;
- automatic real-money promotion.

Never weaken fail-closed correctness, contract mapping, fee correctness, state reconciliation, kill switches, drawdown controls, or causal data requirements to obtain more fills or better backtests.

## 4. Economic objective

Every executable decision must ultimately be evaluated through action-conditional economic value, not quoted edge alone.

For action `a` in state `X`, reason in terms of:

`EV(a|X) = P(fill/joint-state | X,a) * E(capture/alpha - fees - slippage - adverse markout | fill,X,a) - E(partial/inventory/unwind loss | X,a) - capital_cost - latency_cost + conservatively_haircut_rewards_and_rebates`

The action space should converge, where appropriate, to:

`MAKE / TAKE / CANCEL / WITHDRAW / NOTHING`

with risk and liquidation actions having priority over alpha actions.

For multi-leg execution, direct empirical joint completion/state evidence is canonical. Products or minima of marginal fill probabilities are not acceptable generic substitutes.

Queue position affects fillability; it never creates capital capacity.

## 5. Current P0 economic bottleneck

The primary current bottleneck is maker profitability / fillability, not generic infrastructure construction.

The system must learn, causally and action-conditionally:

- `P(aggressive flow reaches our quote before horizon | X,a)`;
- `P(fill before horizon | X,a)`;
- fill-time / survival distribution;
- partial-fill distribution;
- `E(markout_h | fill,X,a)` for multiple horizons;
- inventory duration and unwind loss;
- queue uncertainty;
- effects of JOIN / IMPROVE / FADE / one-sided placement / dwell time / cancel timing;
- regime dependence on OFI, microprice, imbalance, spread, volatility, TTE, flow freshness, fair-value disagreement, inventory and market family.

A high fill rate with adverse markout is not success. A positive quoted spread with no reachable flow is not alpha. A profitable backtest without forward evidence is not completion.

## 6. Continuous-learning doctrine

Codex is not the model that learns. Codex builds and improves the deterministic, versioned learning factory.

The required loop is:

`raw causal events -> immutable provenance -> features -> labels -> dataset cut -> train -> challenger -> chronological OOS -> causal replay -> forward shadow/PAPER -> common-support champion comparison -> operator promotion decision`

New data may automatically produce a challenger. A refit must never automatically become champion.

Every model/policy artifact must bind at minimum:

- strategy/family;
- model/policy version;
- code SHA;
- dataset identity and immutable cut;
- feature schema/hash;
- training start/end;
- label horizon and causal timing semantics;
- hyperparameters;
- fee/cost/reward assumptions and provenance;
- validation window;
- OOS metrics;
- forward metrics;
- decision/promotion state.

Forbidden:

- random shuffled validation for time-dependent evidence;
- future leakage;
- source timestamps overriding receive-time causality;
- silent refits;
- unversioned datasets/models;
- backtest-only alpha promotion;
- selecting the best model on the final holdout;
- changing the sample when applying cost stress;
- counting correlated simultaneous contracts as independent evidence;
- giving exploratory orders promotion credit without explicit causal correction.

Exploration must be bounded, propensity-logged and economically/safety constrained. Preserve enough exploration to avoid policy-induced data blindness.

## 7. Champion/challenger standard

Every material model or policy improvement must be introduced as a challenger first.

Prefer common-support and same-opportunity comparisons. Report incremental value versus the incumbent, not merely absolute challenger results.

Promotion evidence should include, when applicable:

- frozen chronological OOS;
- purging/embargo;
- event/market clustering;
- forward shadow/PAPER observations;
- fill-conditioned markout;
- realized post-cost PnL;
- 1x/1.5x/2x cost stress on the same observations;
- reward/rebate stress including zero-subsidy counterfactual;
- calibration and drift diagnostics;
- fold stability;
- capacity/scaling diagnostics;
- exact code/model/config identity;
- zero causality failures.

If evidence is insufficient, report `MORE_EVIDENCE_REQUIRED` rather than weakening gates.

## 8. External fair-value doctrine

Settlement-aware external fair value is a core P0 capability, but independent information value must be demonstrated before economic promotion.

Preserve:

- exact contract/rules/outcome mapping;
- exact settlement-oracle semantics;
- same-oracle provenance where required;
- receive-time causal ordering;
- external-venue health and disagreement checks;
- probability uncertainty bounds;
- pure-external benchmark separated from Polymarket-price-informed hybrid models;
- calibration by TTE/regime;
- fail-closed behavior for stale/unknown/mismatched inputs.

The target is incremental predictive/economic value over Polymarket and over the current champion, not merely a sophisticated probability model.

## 9. Latency doctrine

Architecture is not latency proof.

Optimize measured end-to-end opportunity capture, not isolated microbenchmarks. Preserve stage timestamps for:

- exchange/source -> receive;
- parse;
- book;
- features;
- fair value;
- decision;
- risk;
- OMS/tx queue;
- serialization/sign/enqueue;
- request -> ack;
- cancel -> ack;
- user/private-stream confirmation.

Track p50/p90/p95/p99/p99.9/max and reconnect/loss health. Regional deployment should be selected by stable tail latency with the same binary/config/SHA.

Do not spend major engineering effort reducing an already-negligible internal stage while network/venue latency dominates.

## 10. Capital and capacity doctrine

Do not allocate capital through arbitrary equal sleeve budgets once sufficient evidence exists.

Move toward marginal allocation using:

- robust net EV;
- uncertainty/confidence;
- volatility/drawdown;
- cross-sleeve correlation;
- executable depth;
- inventory and unwind risk;
- capacity/impact curve;
- regime stability;
- latency sensitivity;
- cost/reward dependence.

Estimate how performance changes with size. A high-Sharpe small-capacity strategy must not be extrapolated linearly.

## 11. Data moat

Treat proprietary causal execution data as a first-class asset.

For every actionable decision, preserve enough provenance to reconstruct:

`state -> model -> action -> intended price/size -> queue estimate -> submission -> ack -> active/cancel state -> public flow -> partial/full fill -> markouts -> inventory -> unwind/settlement -> realized PnL`

Never overwrite raw evidence. Derived datasets must be reproducible from immutable sources.

## 12. Work ordering

Unless current evidence proves a different bottleneck, prioritize:

P0:
1. maker profitability/fillability/action EV;
2. continuous learning and experimentation factory;
3. settlement-aware external fair validation and promotion readiness;
4. unified MAKE/TAKE/CANCEL/WITHDRAW economics;
5. end-to-end venue latency measurement and geographic optimization;
6. canonical execution/risk/reconciliation correctness.

P1:
7. structural/hard/graph execution robustness and capacity;
8. evidence-weighted capital allocator;
9. OSINT and market-open incremental alpha;
10. data/research automation and drift monitoring.

P2:
11. sports latency after verified streaming feed and semantic mapping;
12. cross-platform after second-venue adapter, semantic equivalence and joint race/capital accounting;
13. slower ranking/PCA/local-factor sleeves only when they add common-sample incremental value.

Do not expand P2 while a P0 blocker is unresolved unless the work is truly independent and does not consume critical-path ownership.

## 13. Task protocol for Codex

For every substantial task:

1. Audit current `main` and recent changes first.
2. State the exact bottleneck/hypothesis.
3. Identify existing implementation that should be reused.
4. Define causal/economic acceptance criteria before coding.
5. Make the smallest coherent architectural change that solves the real problem.
6. Add deterministic tests and failure-path tests.
7. Add/reuse telemetry that proves behavior in forward operation.
8. Run the relevant full V7 verification, not only targeted tests.
9. Report what is implemented versus what remains evidence-gated.
10. Do not claim economic success from synthetic tests.
11. Do not mutate unrelated subsystems for aesthetics.
12. Leave the repository simpler or equally simple; remove obsolete surfaces only after replacement evidence is green.

For large missions use the highest available reasoning effort (Extra High when available). Use High for bounded follow-up fixes, CI repair, telemetry/dashboard work and well-specified local refactors.

## 14. Definition of world-class progress

The project is progressing toward the target only when one or more of these improve without hidden deterioration elsewhere:

- robust post-cost forward PnL;
- fill-conditioned markout;
- opportunity capture / fill quality;
- independent information value;
- tail latency and cancellation effectiveness;
- capital efficiency/capacity;
- drawdown/risk-adjusted performance;
- calibration and model stability;
- number of independent forward observations;
- speed and correctness of the research-to-challenger-to-promotion loop;
- operational reliability and reproducibility.

More files, more strategies, more parameters and lower synthetic microbenchmark latency are not sufficient definitions of progress.
