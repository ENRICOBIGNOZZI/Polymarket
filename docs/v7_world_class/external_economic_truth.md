# External Fair economic-truth workflow

The External Fair audit is a read-only SHADOW workflow. It cannot submit an
order, change portfolio equity, promote a model or establish real-money
profitability.

## Evidence collection

`v7_external_fair_paper_router.py` writes an `OPPORTUNITY_SET` for every
distinct causal YES/NO book batch. Each record contains:

- the complete visible books for both tokens;
- exchange and receive timestamps and snapshot identifiers;
- fair point/lower/upper probabilities and settlement identity;
- action-specific fee, execution-risk and robust-EV values;
- the selected `TAKE_YES`, `TAKE_NO`, or `ABSTAIN` decision.

Older tapes predate this event and therefore support only selected-fill replay.
Reports label that scope explicitly and do not infer rejected opportunities.
Offline threshold replay may vary price/cost thresholds, but it never bypasses
contract identity, settlement-reference, oracle, external-data, TTE or
model-disagreement gates. Rejected snapshots remain counted and are excluded
from the executable policy/capacity cohorts.

## Audit artifacts

The economic artifact generator produces four additional files:

- `v7_external_loss_attribution.json`: one reconstructible row per virtual
  lifecycle, with accounting reconciliation, missing causal fields, loss flags,
  and `EXACT_SHA`/`HISTORICAL`/`MIXED_SHA` lineage.
- `v7_execution_latency_distribution.json`: empirical decision-to-arrival
  p50/p90/p99 and stress profiles. Exchange-to-receive deltas are reported as
  book age, not network latency.
- `v7_external_policy_replay.json`: full-depth capacity, frozen exit-policy
  comparisons and threshold/cost/latency grids with day-block lower bounds.
- `v7_exact_sha_economic_bundle.json`: content-addressed raw-tape manifests,
  code/config/model identity, the three reports above, and a verifiable bundle
  SHA-256.

Generate an audit directly from durable evidence:

```sh
python3 scripts/v7_exact_sha_economic_bundle.py \
  --input runs/paper_v7_durable \
  --repo . \
  --config config/v7_external_fair.json \
  --output /tmp/v7_exact_sha_economic_bundle.json
```

Verify that the bundle has not changed:

```sh
python3 scripts/v7_exact_sha_economic_bundle.py \
  --verify /tmp/v7_exact_sha_economic_bundle.json
```

## Interpretation rules

- A dirty worktree is `DIRTY_WORKTREE_HISTORICAL_AUDIT_ONLY`.
- A lifecycle whose entry and terminal events cross revisions is `MIXED_SHA`.
- Old artifacts and rows remain `HISTORICAL`; they are never displayed as the
  current HEAD's runtime result.
- Missing oracle, composite-price, dispersion, depth or latency components stay
  missing. The audit never fills them with zero or a configured constant.
- Positive virtual PnL is not real PnL and never grants promotion authority.
- A policy lower bound is unavailable when fewer than two day blocks exist.

## Settlement-margin model

The primary fair-value runtime no longer contains a fitted bridge coefficient,
fixed conditional variance, or fixed probability band. It extracts causal
features and loads the immutable `btc_5m_settlement_margin_linear_v1` artifact
selected by the model registry. A missing, malformed, wrong-scope, or
wrong-family champion makes the primary fair invalid and authorizes no new
risk. A scalar Platt artifact remains available only as a final challenger
calibration layer; it is not the settlement alpha model.

Build a research dataset over durable forecasts plus archived/live RTDS tapes:

```sh
python3 scripts/v7_external_settlement_dataset.py \
  --input runs \
  --output /tmp/v7_settlement_dataset.jsonl \
  --manifest /tmp/v7_settlement_dataset_manifest.json
```

The builder joins inputs by local receive time, binds the initial and terminal
Chainlink events to the verified five-minute contract boundaries, and predicts
the terminal Chainlink margin relative to the contract reference. The public
RTDS tape exposes the sixty-second TWAP value but not its raw weighted
constituents. Consequently the exact known/future `K_t + U_t` decomposition is
recorded as unavailable and is never synthesized.

Train a research-only immutable artifact:

```sh
python3 scripts/v7_external_settlement_train.py \
  --dataset /tmp/v7_settlement_dataset.jsonl \
  --manifest /tmp/v7_settlement_dataset_manifest.json \
  --config config/v7_external_fair.json \
  --repo . \
  --artifact /tmp/v7_settlement_model.json \
  --report /tmp/v7_settlement_training.json
```

Splits are chronological whole-contract splits, row weights are equalized by
contract, calibration uses validation only, and the test partition is not used
for selection. Publishing a challenger requires an explicit flag and is
refused from a dirty worktree. Training and validation never promote a model.

Forward validation uses only observations strictly after the artifact's frozen
publication boundary:

```sh
python3 scripts/v7_external_settlement_validate.py \
  --artifact /tmp/v7_settlement_model.json \
  --dataset /tmp/v7_settlement_dataset.jsonl \
  --config config/v7_external_fair.json \
  --output /tmp/v7_settlement_validation.json
```

Manual promotion remains blocked until all predeclared gates pass: 30 forward
days, 2,500 independent settlement contracts, 300 executable policy actions, a
positive day-block 95% lower bound, local calibration uncertainty below the
claimed edge, and zero causality failures. Forecast labels continue to be
collected in `SHADOW_SETTLEMENT_FEATURE_ONLY` mode when no champion exists, so
the cold start cannot suppress its own training evidence.

## Maker randomized shadow probes

The existing maker runtime is still the sole runtime and OMS. Its predeclared
information/economic, placement, side, and lifetime randomization is now
mirrored into canonical `SHADOW_PROBE` lifecycle records. These records carry
the exact assignment propensities and are explicitly
`SHADOW_ZERO_AUTHORITY`, `excluded_from_portfolio_equity`, and unable to enter
the execution queue or reserve capital. Assignment, quote-live, cancel,
rejection, partial-fill, and terminal-fill phases share the causal candidate
and order identities.

`v7_maker_durable_learning.py` preserves compatible probe rows and reports the
chronological forward-tail policy value of the predeclared `ECONOMIC` arm using
a Horvitz-Thompson estimator with whole-day bootstrap bounds. Predictive MSE
improvement alone cannot activate the learned placement policy. The additional
economic gate requires at least 10,000 forward-OOS quote episodes, 300 fills,
100 market-day clusters, complete causal markouts, and a positive day-block
95% lower bound. Rewards remain zero unless observed and attributable to the
system's own fills.

## Fast Structural feasibility

The canonical runtime launches Fast Structural as a shadow-only policy
evaluator. For every structurally valid candidate it records the complete
arrival books, fee schedule, full-depth `EV(q)` curve, finite-notional check,
`q*`, and the configured-linear diagnostic `tau*`. It owns no PAPER capital,
inventory, order/fill authority, or ledger execution authority.

Generate the feasibility funnel and compare it with empirical p99 latency:

```sh
python3 scripts/v7_fast_structural_feasibility.py \
  --input runs \
  --latency-report artifacts/v7_execution_latency_distribution.json \
  --output /tmp/v7_fast_structural_feasibility.json
```

The report follows `detected -> structurally valid -> full-depth positive ->
positive after fees -> positive after latency -> all legs filled -> terminal`.
It recommends a freeze when p99 latency exceeds `tau*` for at least 80% of 20
measured opportunities. It never mutates strategy state and requires 50
complete terminal bundles before eligibility can be considered. Infinite
notional sentinels have been replaced by finite $300 defense-in-depth limits.

## Robust advisory allocator

`v7_evidence_capital_allocator.py` now separates information budget,
exploitation, and cash reserve. Its default proposal keeps at least 85% cash;
exploitation starts at zero and requires all of the following:

- at least 300 terminal units by default, with strategy-specific overrides;
- at least 30 whole-day stressed-PnL blocks;
- positive 95% day-block bootstrap lower bound and positive 2x-cost PnL;
- finite executable capacity and positive capital-hours;
- no hard drawdown breach.

Eligible scores are penalized by shrunk day covariance, capped by capacity,
25% strategy concentration, and a 25% step-up from the current envelope. The
result is content-addressed, advisory only, and requires a separate manual
promotion artifact; it never transfers capital automatically.

Training features use the same recorded live multi-venue composite, returns
and age fields consumed by runtime inference; legacy Binance-only rows are not
silently mixed into that feature schema. Economic validation selects at most
one first qualifying position per settlement market, requires visible size at
least the venue minimum, and computes conditional calibration uncertainty on
independent-market clusters with Wilson intervals.
