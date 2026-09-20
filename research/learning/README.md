# Cumulative V7 PAPER learning

This is the authoritative daily learning entrypoint. It inventories immutable
history, constructs causal signal and decision populations, and publishes private
research reports. It has no execution, ledger-writing or deployment authority.
The legacy `v7_fit_probability_candidate.py` remains a feature-ABI compatibility
adapter and historical exploratory tool; its submitted-order sample is not the
training population of this pipeline.

## Three settlement forecasters

The requested scope is **exactly three**: unchanged PM probability, regularized
logistic correction to PM logit, and a small histogram gradient-boosted correction
to PM logit. The latter fits a bounded Newton residual about PM. Logistic asset
and horizon effects share a regularized fit rather than 30 independent fits.
There are no spline, MLP, sequence or additional residual-model contenders.
Execution quantity, joint quantity × payoff, cost and net-value predictions are
separate action-conditional regressions, not additional settlement forecasters.

All preprocessing fits on the chronological training partition. Missing values
retain explicit indicators; absence in raw evidence is never encoded as an
observation of zero. Calibration compares raw, Platt, temperature and isotonic
maps on a distinct chronological calibration partition. Hyperparameters and
weighting use inner expanding folds; family comparison uses outer expanding
folds. The final audit does not choose a family or fit calibration. Entire
markets crossing boundaries, overlapping contract lifetimes and labels not yet
available are purged. A five-minute embargo applies on both sides. Dependence
uncertainty uses settlement-day blocks, with equal market weight within blocks.
It is a diagnostic interval, not certified conditional coverage.

The training comparison requires at least 30 unique markets and four observed
day blocks even for a descriptive fit. PAPER eligibility requires at least 100
markets and 14 blocks plus all independent probability, execution, context,
native-runtime and risk gates. Calendar span is not a substitute for observed
days. Scarce data produce `INSUFFICIENT_DATA`, not a fabricated winner.

## Private setup and daily operation

Use Python 3.12 or newer in a research-only virtual environment:

```sh
python3 -m venv /private/path/venv
/private/path/venv/bin/pip install -r research/requirements-learning.txt
```

Create a private root outside the public repository, mode 0700. Put these files
there (the hostname must be the actual authorized research machine):

```json
{"schema":"v7_research_host_v1","hostname":"RESEARCH_HOSTNAME","role":"RESEARCH_ONLY"}
```

Save that as `research_host.json`, and configure `settings.json`:

```json
{
  "source_roots": ["/private/history", "/private/offload/current"],
  "sync_london": false,
  "fetch_public_settlements": true,
  "execution_scenario": null
}
```

Roots are explicit and cumulative. Add all authorized generations, closed tapes,
archives and offloads. Sources are read without modification. The catalog records
unsupported sources rather than guessing a decoder. Gzip and tar members are
read without extracting archive paths. Compatible JSON/JSONL sources are copied
into the existing content-addressed evidence store. Reconstruction reads only
hash-verified revisions; mutable files and SQLite indexes are not authority.
All prior catalog revisions participate, including sources no longer present.

For London sync, set `sync_london=true`, configure the existing
`POLYMARKET_LONDON_HOST`, `POLYMARKET_LONDON_USER`,
`POLYMARKET_LONDON_RUN_ROOT` and `PM_V7_RESEARCH_SYNC_ROOT` environment variables,
and include the sync root's `current` directory in `source_roots`. The existing
verified offload receipt protocol is retained. A failed transfer does not become
an empty success. Public Gamma settlement responses are frozen with their actual
fetch time. Responses fetched after today's frozen cutoff enter the next daily
cutoff; they are never backdated.

```sh
PM_V7_RESEARCH_ROOT=/private/learning \
PM_V7_RESEARCH_PYTHON=/private/path/venv/bin/python \
  bash research/install_macos_scheduler.sh
```

The macOS launcher wakes every minute and at login. Python computes midnight in
`Europe/Zurich`, independently of the machine timezone and across DST. A lock and
immutable date receipt prevent duplicate work. A missed midnight catches up at
the latest midnight on next availability. Linux research hosts can instantiate
`ops/systemd/polymarket-v7-research.{service,timer}.in`; the timer is persistent
and has an explicit Zurich timezone. Enrollment rejects London runtime hosts.

Manual invocations use the same idempotent daily entrypoint:

```sh
/private/path/venv/bin/python -m research.learning.daily --root /private/learning
```

Completed daily receipts are never overwritten. An unchanged resolved training
population yields `NO_NEW_TRAINING_INFORMATION`. Failed attempts have separate
immutable receipts and remain retryable. Code SHA and implementation hashes
record dirty work explicitly. Source catalogs, dataset manifests, separate
signal/decision views, reports, candidates and registry transitions are private.
`status.json` is a replaceable pointer to the latest immutable daily receipt.

## Labels, execution and policy

Native rows include rejected and accepted evaluations, with explicit signal and
decision identity. The signal view chooses the first causal decision for a
trigger, avoiding thousands of repeated evaluations dominating the sample.
Optional features without causal information-time evidence remain missing.
Legacy rich forecasts with proven feature cuts form a separate incompatible
stratum. Unresolved legacy and native outcomes remain `PENDING`; unsupported
legacy feature timing is excluded with a reason. Public labels validate source
response hashes and settlement facts. Training cannot see future labels.

Execution replay requires exact terms, fees, an explicit positive transport
delay, quantity and execution-cost assumption, intact sequence/epoch history,
a healthy closure watermark, and causal arrival-book coverage. Only proven
`FULL` captures are currently eligible: older decision-window logs do not encode
the coverage interval needed to establish every rejected signal's arrival state.
Missing coverage or costs are censored. Nonfills and partial fills require actual
replay evidence. Actual price already includes slippage, so slippage is reported
without a second deduction. Cost reserves marked `measured=false` are assumptions.
No replay writes canonical cash or a ledger.

The six research policy comparisons are current baseline, PM-only, raw shock,
probability-only, probability plus execution, and uncertainty-adjusted EV. The
threshold is frozen in the research policy before the final audit. Each policy
selects at most its first action per market. PnL is research replay PnL; portfolio
capital replay and transportability are separate promotion requirements. The
present trainer does **not** certify these requirements: it records them as false
and rejects eligibility until independent evidence exists. A successful fit is
not a successfully validated tradable policy. The live 105–120 second window,
3.75 USD order ceiling, 20-share ceiling and 7,200-second cohort remain unchanged.

## Native artifacts and promotion

The logistic native export reuses the existing 18-feature ABI, fitting only
causal native rows. Its smaller feature contract needs a separate untouched OOS
audit; performance of a richer offline model cannot authorize the export. The
artifact includes exact code/data identities, context encoding, calibration,
day-bootstrap plus penalized-curvature uncertainty, fixed risk ceilings and
PAPER-only flags. Gradient boosting stays in the research plane. No scikit-learn,
NumPy or Python fitting dependencies enter London's runtime bundle.

`registry.py` records immutable transitions. It has no deployment client.
`TRAINED` is followed only by explicitly evidenced transitions to
`VALIDATED_OFFLINE`, `PAPER_ELIGIBLE`, `PAPER_FORWARD_TEST`, `PROMOTED` or `REJECTED`.
Even several positive hours cannot automatically promote a model.

The existing `POLYMARKET_PROBABILITY_MODEL_SOURCE` cutover boundary now requires
an adjacent `<model>.promotion.json` with schema
`v7_probability_promotion_proof_v1`. `scripts/v7_probability_promotion.py` validates
exact model/report bytes, code/dataset identity, required gates, native parity,
actual native OOS probability statistics, all-context support and conservative
economic support. Native parity must cover at least 30 cases with maximum error
at most 1e-9 and zero hot allocations. Economic support requires positive day-block
lower-bound PnL, 100 markets and 14 day blocks; boolean assertions alone fail.
The report must have no rejection reasons and be `PAPER_ELIGIBLE`. The proof and
report are private, colocated and installed with mode 0600. Validation happens
before stopping a healthy runtime and again after installation. Midnight jobs
never construct activation requests, push models or reset a cohort clock.

## Monitoring and verification

The native cold evidence writer counts each observed signal once per stage per
market/capture. Raw decision reason counts remain evaluations, not unique
triggers. Generation validity is unknown and reported as null. Counts represent
stages ever reached and are not assumed to be a strictly decreasing funnel.
Grafana retains crypto-only navigation and adds configured/open probability,
unique-signal stages, research cutoff and candidate-state diagnostics. The
exporter reads a separately synchronized `control/research_training_status.json`
when available; missing research status does not become zero training data.

```sh
OMP_NUM_THREADS=1 OPENBLAS_NUM_THREADS=1 /private/path/venv/bin/python -m pytest \
  tests/test_v7_cumulative_learning.py tests/test_v7_learning_models.py -q
ctest --test-dir build --output-on-failure -R \
  'pm_v7_(probability_model|crypto_decision_lane|native_runtime_evidence)_tests'
build/pm_v7_probability_model_tests --benchmark
```

The benchmark emits p50/p90/p99/p99.9 for batched feature construction and native
inference using synthetic inputs, with zero-allocation assertions. It is a local
microbenchmark, not London signal-to-arrival latency. Actual admission/arrival
latency and forward performance require the exact deployed runtime and its
private observations. No native artifact, open probability gate, profitable
policy or successful deployment is claimed merely because these tests pass.
