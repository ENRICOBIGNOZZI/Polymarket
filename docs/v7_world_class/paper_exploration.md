# V7 PAPER_EXPLORATION authority

`PAPER_EXPLORATION` permits verified external information to generate simulated BTC M5 order and fill lifecycles inside the live V7 PAPER runtime.

The authority is deliberately narrow:

- the global portfolio coordinator is the only selector;
- a matching deterministic replay receipt is required before simulated execution;
- the arrival CLOB book is revalidated before every PAPER fill;
- the settlement binding and rules hash must be verified;
- immature evidence receives no promotion credit;
- canonical PAPER accounting is written through the V7 ledger spool;
- `new_risk_authorized=false`;
- `authenticated_execution=false`;
- `real_order_submission=false`;
- `real_capital_at_risk=false`.

A missing receipt, stale input, invalid settlement binding, failed arrival revalidation, non-positive conservative EV, or breached PAPER cap results in `NOTHING` and fail-closed accounting.

This authority changes the live PAPER evidence path only; it creates no authenticated venue authority and no real-capital path.

## Cold-start fair-value bridge

When no immutable trained settlement champion has been published, BTC M5 may use
`btc_m5_same_oracle_diffusion_bootstrap_v1` **only** inside
`PAPER_EXPLORATION`. The bridge uses the exact Chainlink opening reference,
current Chainlink TWAP, a fresh multi-venue composite, short-horizon innovations,
time to expiry and cross-venue dispersion. It produces a conservative probability
interval rather than a point forecast.

The bridge is pinned by the deployed code SHA and a canonical policy hash. It is
not a trained champion, cannot receive promotion credit, cannot authorize maker
quotes, cannot authorize authenticated execution, and cannot submit real orders.
A valid immutable registered champion always takes precedence.

## Release invariant

Every deployment containing the bridge must pass exact-SHA Release and Debug CI,
retain the PAPER-only authority flags, and be rechecked against live Chainlink,
multi-venue and CLOB inputs after the runtime starts. A successful build alone is
not evidence of economic activity or profitability.

## Bounded information-gain micro-probes

A valid bootstrap fair may be too uncertain for a positive lower-confidence-bound
trade. In that cold-start state V7 keeps robust trading unchanged and may submit
at most one separately labelled BTC M5 PAPER probe per contract. A probe requires
positive point-estimate EV after authoritative fees and execution risk, a material
but bounded model-market disagreement, fresh arrival-book revalidation and a hard
maximum loss of 2 USD (also capped at 5 basis points of the engine sleeve).

The coordinator always prefers positive robust wealth change. Only when no robust
candidate exists may it issue `paper_exploration_probe_authorized=true`. Probe
PnL is canonical PAPER evidence but has `promotion_eligible=false`; it cannot be
used as proof of a mature edge and never grants authenticated or real-money
authority.



## Operator-approved capacity and frozen economic comparison (2026-09-05)

The operator explicitly changed `critical_free_ratio` from 0.10 to 0.04.
The absolute 5 GiB minimum and 20% warning are unchanged. Lowering this
threshold does not free storage or demonstrate profitability.

Per-venue raw and normalized C++ recorders now close segments at 64 MiB or
300 seconds in the writer thread. Each segment has its own session header;
record sequence numbers do not reset. A complete segment is flushed, closed,
fsynced, and renamed from `.bin.open` to `.bin`. A crash leaves `.open` evidence
untouched. Legacy callers that do not request segmentation retain their API.
The existing retention service runs every five minutes and may compress only
closed, sufficiently old `.segment-NNNNNN.bin` files in the active run, as well
as inactive cutover tapes. It verifies a full gzip round trip before retirement.
No ledger, live open tape, or unrelated project is an eligible source.

The previous calibration trainer filtered v1 evidence although the router
produces v2. The replacement uses verified v2 FORECAST/FORECAST_FINAL pairs,
matching market/token identities, exact binary public settlement assertions,
and labels available strictly before freeze. Sources are identified by complete
prefix length and SHA-256; selected training rows have their own content hash.
A ridge-regularized logistic residual is fitted with equal market weights.
It corrects structural logits using oracle/reference basis, spot/oracle basis,
short external returns and the fraction of the final window observed. Feature
scaling is fitted only on training data. Polymarket prices are not model inputs.

The existing registry publishes this as a CHALLENGER only. It does not replace
the champion or grant execution authority. Its parameter hash, policy hash,
training identities and protocol are frozen; reuse never silently refits it.
Forward observations start with the next full contract after publication.
The monitor emits its point probability separately and marks its probability
interval as unvalidated. The router binds that prediction and feature vector
to each complete observed opportunity/book snapshot in the existing tape.

Run the independent, zero-authority comparison without altering the account:

```sh
python3 scripts/v7_external_fair_challenger.py --evaluate \
  --tape runs/paper_v7_durable/external_fair/counterfactuals.jsonl \
  --tape runs/paper_v7_live/external_fair/counterfactuals.jsonl \
  --registry runs/paper_v7_live/external_fair/model_registry \
  --config config/v7_external_fair.json --model-sha "$(git rev-parse HEAD)" \
  --status runs/paper_v7_live/reports/frozen_economic_comparison.json
```

The market, structural, and challenger cohorts use the same predeclared rule:
2 USD maximum debit, one attempt per contract, 4-cent point EV after fees and
an execution-risk allowance, a 250 ms assumed arrival delay, an observed
arrival no more than two seconds later, and a conservative visible-depth
fraction. The initial ask is the limit price. Missing arrival data is NONFILL,
not an interpolated fill. Token/fee drift or stale books are non-executable.
The report includes every research decision's source record ID, simulated
arrival/fill identity, fees, independently observed settlement and reconciled
hypothetical cash. These are snapshot-based research outcomes, NOT venue
fills, NOT canonical PAPER orders and NOT probe performance. Probability
losses cannot be substituted for trading PnL, and no automatic promotion exists.
