# V7 algorithms

The live PAPER registry contains exactly two algorithms:

- `CRYPTO_SETTLEMENT_ENGINE`, with settlement-fair, professional-maker and informed-taker components.
- `STRUCTURAL_ARB_ENGINE`, with hard-arbitrage and fast-structural components.

Components are implementation details and have no independent authority. A third algorithm, an unknown identifier or a component promoted to algorithm status fails the startup and monitoring contracts.

## Frozen, rich external-information PAPER model

The single CRYPTO_SETTLEMENT_ENGINE fair owner can run `btc_m5_rich_external_logit_v1`.
Its target is the verified BTC M5 binary settlement; it compares an independent
external-information logistic model with a logistic correction to the causal
Polymarket prior. Features include oracle/spot margins and time interactions,
spot microprice, cross-venue dispersion, order flow and observed volatility.
Perpetual basis, funding, OI velocity and option IV are recorded with their
receive-time provenance, and are fitted only when historical training coverage
supports them. Optional absence is explicit, not a fabricated zero.

The previous 256-event return history has been replaced by 10ms time buckets.
Unavailable history is published as null with an availability bit. Historical
pre-fix returns are excluded from the new model rather than treated as flat prices.

Startup training uses settled original forecast cuts, label-availability embargoes,
whole-market train/validation/audit splits and equal-market weighting. Six compact
models compete on validation Brier/log loss. Audit results are diagnostic only.
A new immutable CHALLENGER is bound to code/policy/data hashes and a future whole
contract boundary. No training, network request or model mutation occurs inside
its inference function. No automatic champion promotion is introduced.

Its uncertainty interval remains [0,1] until independently validated. It can
therefore enter ONLY the existing loss-capped, zero-promotion-credit PAPER probe
lane, never mature robust MAKE/TAKE authority. The verified external-cancel rule
is unchanged and remains a separate risk action. A failed or absent learned model
leaves the declared bootstrap fallback visible; it is not silently called ML.

### Immutable Maker publication during evidence refits

The durable Maker learner materializes the execution-model snapshot once per
exact-SHA/policy/config run. Periodic fitting updates evidence and the separate
challenger flow; it must never overwrite the published champion, including its
generation timestamp. The external-cancel protocol refuses any subsequent change
to that identity. Newly decoded episodes pass the same causal validator before
admission. Sessions which started before publication are not forward evidence.

The prior mutable publication timestamp invalidated 2,525 diagnostic episodes in
142 old-runtime files. These must be quarantined with content hashes, never
backdated or credited toward promotion. The independently hash-pinned official v3
seed is separate evidence and must still pass full recomputation.
