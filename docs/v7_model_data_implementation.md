# V7 model and data implementation checkpoint

Base: `037a57758c39cfb2dccb36580b6ca04fd90e7f69`.
Scope: the first offline P0/P1 slice of the crypto model/data roadmap.
This is implementation, not a new live model, collector, OMS or execution owner.
Nothing in this patch changes the current runtime, risk limits, model registry,
PAPER authority, collection schedule or storage authorization.

## Implemented

`scripts/v7_research_economic_contract.py` provides immutable market economics,
explicit availability-clock identities, fee and delay scenarios, certified
as-of book lookup, price-limited visible-depth sweeps and round-trip labels.
Both the entry and exit include latency and market delay. The caller must state
whether an observed latency already includes the venue delay. It is never
silently added twice. Rates and delays have no crypto-wide defaults.

The entry limit is part of the recorded decision. The exit policy observes the
bid at its decision and freezes that limit before the exit delay. FAK and FOK
are distinct. An incomplete liquidation leaves inventory and an unavailable
terminal PnL, rather than an invented zero. Cash change is not called profit
while inventory remains.

A quiet book can support a label only with continuous coverage certified by its
producer. Mere absence of updates is not such certification. The lookup uses
feature/book availability time, not the source timestamp or the first receipt
of an event which has not yet been applied. No future book interpolation is
allowed. Host/boot clock domains cannot be compared without an upstream verified
mapping. In this first implementation a dataset must have one clock domain.

`scripts/v7_research_causal_dataset.py` validates explicit evidence bundles,
builds examples or separate exclusion records, and publishes a bounded JSONL
file and a content-hashed manifest without overwriting earlier evidence.
Input files must be sealed. Duplicate JSON keys, nonfinite values, duplicate
bundles, incomplete lines and source mutations are rejected.

The purged chronological split keeps entire markets and connected shared
calendar blocks in the same partition. Labels must be available before the
next partition, including the embargo. Censored positions do not become zero
training targets. The resulting rows are counterfactual decisions, NOT a
tradable portfolio return series: overlapping decisions require the existing
single allocator and inventory simulation before portfolio PnL is meaningful.

A read-only retention admission helper respects a maximum 60 decimal GB and
never authorizes deletion or external storage. It is a snapshot preflight,
not a replacement for the canonical retention owner's atomic admission.

## Explicit limitations and required evidence

These labels are **visible-book counterfactuals**, not observed exchange fills.
The model assumes exogenous snapshots and makes no queue-priority, matching or
self-impact guarantee. Public displayed liquidity is not reserved for us.
Price improvement or fill priority must not be inferred as a real observation.

The implemented fee formula is `C * rate * p * (1-p)` with an explicit rate
from the recorded market economics. Fees are unrounded research estimates per
visible price level. This is not exchange fee-rounding/fragmentation parity,
and rebates are not assumed. Exact fill-level fee and collateral treatment
must be validated against the applicable market version before a claim about
real execution. Unsupported fee formulas are rejected, not coerced.

`valid_until_ns` must be no later than the market close and the end of the
metadata version's validity. A known subsequent economics change invalidates a
label crossing it, even if a later frame switches back to the earlier identity.
Rules, settlement semantics and raw metadata source hashes remain separate.

`CoverageProof` is a contract to be supplied by a verified producer, not a proof
created merely by setting a boolean. The producer must invalidate it on any
loss, reconnect, unresolved sequence gap or incomplete depth reconstruction.
Its hash must resolve to preserved evidence. These checks and the adapter from
canonical raw/normalized tapes have NOT yet been connected to the live runtime.
Do not manufacture those fields for historical data that did not record them.

Different models/policies may share raw data only when the data semantics match.
A code SHA is not a substitute for a probability-model, feature or policy hash.
The existing rich-settlement and PM-repricing trainers remain the training path;
this patch does not duplicate them or create an automatically promoted model.

## Use on an explicit sealed bundle file

The input schema is `polymarket_v7_executable_evidence_bundle_v1`. Required keys:
`market`, `decision`, `books`, `coverage`, `entry_latency`, `exit_latency`,
`holding_ns`, `features`, `calendar_block`, `market_open_ns`, and `schema`.
The dataclasses define the exact fields. Decimal values should be serialized as
strings. `features` must match the immutable decision's `feature_hash`.
The tests provide small deterministic synthetic examples; they are not live data.

After canonical storage-owner admission, with an existing output directory:

```sh
python3 scripts/v7_research_causal_dataset.py \
  --input runs/research/closed-bundles.jsonl \
  --output runs/research/executable-examples.jsonl \
  --maximum-input-bytes 67108864 \
  --maximum-output-bytes 67108864
```

The CLI performs no network request. The data file is published first and its
manifest last. Consumers must require the valid manifest and verify both hashes.
A crash can leave an orphan data file. Recovery must inspect it explicitly;
rerunning never silently overwrites it. Per-file limits do not substitute for
the existing aggregate 60 GB limit. No source file is deleted by this builder.

## Deterministic validation

```sh
python3 -m unittest discover -s tests -p 'test_v7_research_*.py' -v
python3 -m py_compile scripts/v7_research_economic_contract.py \
  scripts/v7_research_causal_dataset.py
```

The existing CMake test glob and canonical verifier already discover these test
filenames; no extra CI/deployment workflow or runtime process is introduced.
The isolated test suite includes fee reversal, delay double-count prevention,
no-chase execution, partial liquidation, metadata transitions, clock domains,
future features, quiet-versus-uncovered books, grouping, label embargoes,
immutable publication, corruption and input/output size limits.

## Next work and stop conditions

`config/v7_model_data_roadmap.json` records every remaining work package, its
state and next gate. Offline implementation is not runtime completion.

The remote development terminal reported its monthly Desktop Commander quota
exhausted during this work. Do not retry/reconnect to bypass that restriction.
Live source adapter, London inspection and deployment are therefore pending.
Development continues through an isolated GitHub review branch and deterministic
local tests; no operational credentials or runtime data are copied into Git.

Before review/merge, run the full exact-head CI and `scripts/verify_v7.sh` in a
clean full checkout. Before runtime wiring, replay a real recorded episode and
compare with the existing canonical economic journal. Before model promotion,
complete the frozen out-of-sample/forward gates. None is waived by unit tests.

## Specification references

Polymarket fee documentation, checked 2026-09-19:
https://docs.polymarket.com/trading/fees

Polymarket order lifecycle and market metadata are version-dependent. Delay
values must be captured from the applicable market, not copied from prose into
an implicit runtime default. The 250 ms and 0.07 values in tests are explicit
synthetic fixture parameters, not an assertion about every traded market.
