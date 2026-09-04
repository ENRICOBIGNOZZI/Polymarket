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
maximum loss of 5 USD (also capped at 25 basis points of the engine sleeve).

The coordinator always prefers positive robust wealth change. Only when no robust
candidate exists may it issue `paper_exploration_probe_authorized=true`. Probe
PnL is canonical PAPER evidence but has `promotion_eligible=false`; it cannot be
used as proof of a mature edge and never grants authenticated or real-money
authority.

