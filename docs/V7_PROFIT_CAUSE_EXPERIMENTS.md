# Economic attribution and prospective PAPER experiments

This implements the profit-cause diagnosis approved on 8 September 2026.
Completion must cover the whole protocol, not just telemetry or a higher fill
count. The canonical execution ledger remains the accounting authority; the
existing coordinator and native executor remain the only execution path.

## Required deliverables and acceptance evidence

1. Separate simulation fill events from positive-quantity operational fills.
   Test a scenario event with zero operational quantity followed by a real PAPER
   fill, and reconcile telemetry to the emitted canonical records.
2. Attribute every historical closed position by actual token, model, probe
   status, decision/arrival evidence, quantity, fill price, realized costs and
   payoff. Reconcile the decomposition to canonical final PnL using decimal
   arithmetic; preserve explicit missing-data reasons and never infer a missing
   probability from PnL. Maker rows also carry queue, duration and markouts.
3. Report funnel transitions per distinct opportunity and usable learning
   examples per exclusion reason, with separate attempts, orders, quantities,
   markouts and final positions. Preserve model/probe/component strata.
4. Freeze signal-region bins, time-to-settlement bins, selection, cost stress,
   delay grid and model identity before the prospective period. Compare against
   PM on the same contracts. Use received book evidence and available depth;
   missing continuity, depth or costs must censor executable-return estimates.
5. Run a controlled research comparison with the existing native PAPER engine: JOIN 5s
   control, observed-flow selection, longer lifetime, and IMPROVE1 only where
   spread/tick/post-only and existing authorization permit it. Pair all variants on the first canonical order per contract, with immutable
   protocol identity. Use a common quantity at most the anchor quantity, capped
   at the worst variant price under the existing loss allowance. These are
   separate research results, never new authorizations or canonical fills.
6. Evaluate selected signal value, decision-to-arrival decay, operational fill
   quantity, fill-conditioned markout and net PnL together. Report uncertainty
   and insufficient evidence explicitly; no manual narrowing of probability
   bounds, automatic sizing increase, or promotion from inspected outcomes.
7. Verify deterministic contracts, canonical local/remote gates, exact-main
   deployment, single-writer/PAPER invariants and actual forward records in the
   live system. Publish the historical attribution and prospective report with
   source hashes, protocol identity, sample scope and limitations.

Implementation and validation evidence are recorded in the task report. A lack
of profitable outcomes is not itself a software failure; missing measurement or
an experiment that cannot distinguish the named hypotheses remains unfinished.

## Runtime and interpretation

The existing receive-time lead/lag collector also collects the experiment; no
additional execution owner or capital path is introduced. It freezes
`config/v7_profit_experiment.json` in
`$DURABLE_ROOT/profit_experiments/$SHA/manifest.json` before the next five-minute
boundary. Model, code, bins, delays, costs, selection and inference settings are
part of the immutable hash. A changed model is excluded until a new protocol
identity is deliberately installed. Restarts retain selected cells and completed
labels; unavailable book history is censored instead of reconstructed.

`observations.jsonl` contains selections, delay labels, first-order anchors and
four-arm native research comparisons, with received book cuts and public trade
paths. Book frames now embed their actual trade payload in the same continuous
sequence, preserving gap detection. Maker replay uses the native arrival and
public-trade monotonic clocks, canonical pessimistic queue multipliers, the
5/10-second expiry and 100ms cancel latency. Common quote quantity is reduced
when necessary to preserve the original loss allowance at the highest arm
price. Flow selection can abstain. A crossing IMPROVE1 quote is ineligible.

These replay fills are research counterfactuals and **never canonical fills**.
The observed real PAPER fills remain in the execution ledger. L1 plus aggregate
features does not establish full-depth executability, true queue position,
market impact or minimum-order-size eligibility for the one-share signal price
diagnostic. Midpoint and bid markouts are separately named; insufficient bid
quantity does not imply executable liquidation. Maker settlement sensitivities
include the predeclared risk allowance separately from the zero maker entry fee.

The existing economics loop publishes `profit_attribution.json`/CSV and
`profit_experiment_report.json`. `/profit-attribution.json` and
`/profit-experiments.json` expose the cached reports through the exporter. Report
and collector timestamps identify freshness. Public binary settlement labels
retain the source response, endpoint and hash. Unknown, unsettled and conflicting
evidence cannot produce a resolved score.

All fixed signal cells are reported, including empty cells. Repeated observations
are averaged within contract before inference; the 20,000-draw seeded bootstrap
uses a Bonferroni correction for the full fixed comparison family. Below 12
contracts there is no interval. Intervals are approximate and three chronological
fold means expose instability; correlated market regimes remain a limitation.
No report grants promotion or changes sizing or unvalidated probability bounds.

Historical attribution reads explicit decision probabilities. For legacy Taker
records, preserved `fair_yes` and outcome recover the token point forecast; the
old `point_probability` field could instead contain the conservative bound.
Legacy model stage ambiguity remains visible and no missing model is guessed
from nearby forecasts. Decimal cash and probability decompositions have separate
reconciliation flags, each checked to a microdollar. Entry fees are counted once;
modeled slippage already represented in fill price is not charged again.


## Protocol v2: distinguish trade inputs from book-state observations

The first v1 forward Maker window contained 81 valid public prints and 29
intermediate book invalidations; no print had invalid lineage. V1 censored all
four variants because it required every book update through 42 seconds to be
valid. This was stricter than the native queue engine's input contract: a
resting quote advances on public prints and timers, not subsequent book deltas.

Protocol v2 keeps the arrival-book, feature freshness, tick, transport/session,
sequence and status gates. Every trade consumed during each arm's own lifetime
and cancel window must retain valid lineage. Missing payloads or invalid prints
censor that arm. Intermediate book invalidations are counted explicitly; a
markout whose as-of book is invalid remains missing, even if a later snapshot
recovers. No later book is substituted for the target. This changes research
observation eligibility only, not canonical PAPER execution or fee assumptions.
V1 outputs and their identity are retained. V2 begins under a new immutable
manifest and future boundary; old comparisons are not relabelled as v2 evidence.

V2 writes each full book path once as an immutable gzip sidecar addressed by its
uncompressed SHA-256. The observation includes its relative path, compressed
hash, byte counts and row count. Compression is verified before publication.
The collector's restart scan and the minute report read the compact observation
stream; they do not repeatedly deserialize tens of megabytes of book evidence
per contract. Sidecars retain the complete replay input for audit and export.
