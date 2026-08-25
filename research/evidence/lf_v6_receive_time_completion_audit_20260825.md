# V6 LF receive-time completion audit — 2026-08-25

## Research state

`MORE_EVIDENCE_REQUIRED`

Exact audited execution code: `7b31e9c6384264fda12e69f3d4b1a658039d0f9b`; the successor `main` commit `7a116a4278f9195eceb2f2d4479a06f402c60a68` changes post-merge recovery only and leaves the audited recorder/broker path unchanged.

## Structural defect

The production public trade recorder persists both the exchange/event timestamp (`timestamp`) and the local observation timestamp (`received_ms`). The V6 multi-leg paper broker reads the same tape but drops `received_ms`, converts only the second-resolution event timestamp to milliseconds, and uses that derived time to decide whether a passive order was active when the trade occurred.

For paper execution this is not a causal clock. A Data-API trade with an exchange timestamp inside an order lifetime can be observed locally only after the order was already cancelled or its execution deadline passed. Conversely, a trade observed just after an order arrival within the same wall-clock second can have an exchange timestamp that rounds to before the order arrival. Both cases misclassify queue depletion and therefore completion, partial-fill and unwind PnL.

This is orthogonal to the point-in-time book-state defect isolated in #336. Even with correct historical queue/depth snapshots, Graph/RV completion must use the timestamp at which public flow became locally observable.

## Deterministic counterexamples

### Delayed observation / false fill

- order arrival: `99.500s`;
- cancellation effective: `103.500s`;
- trade exchange timestamp: `100.000s`;
- trade locally received: `105.500s`;
- queue ahead: `20` shares;
- compatible trade: `30` shares;
- own remaining size: `10` shares.

Event-time replay declares the trade eligible, consumes the queue and fills all 10 own shares. Receive-time causal replay rejects it because the trade became observable two seconds after cancellation.

### Same-second ordering / false non-fill

- order arrival: `100.800s`;
- trade exchange timestamp: integer second `100`;
- trade locally received: `100.900s`.

Event-time replay places the trade at `100.000s`, before arrival, and rejects it. Receive-time replay correctly recognizes it as post-arrival observable flow.

## Fresh tape measurement

The artifact from exact-main live smoke run `32871116566` contains 259 tape rows. Because the recorder stores both clocks, the age of a row when it was appended can be measured directly:

- median `received_ms - timestamp*1000`: about **439.9 seconds**;
- maximum: about **898.9 seconds**;
- **249/259 = 96.1%** of rows were appended more than 60 seconds after their exchange timestamp;
- **211/259 = 81.5%** were appended more than 180 seconds later.

Important interpretation: this smoke intentionally starts the recorder with a **900-second lookback before posting the Graph/RV bundles**. These numbers therefore measure the deliberate backfill age in this smoke, not steady-state Data-API network latency. They should not be read as an estimate that 81.5% of live trades are normally delayed by more than three minutes.

They are still directly relevant to causality: the execution consumer reads an append-only polled tape, so restart/backfill, a delayed batch, or a transient collector failure can append an old exchange-time row after an order has already progressed. Event-time-only gating can then place that newly observed row into an earlier order lifetime. `received_ms` is already persisted precisely enough to prevent that leakage.

For the nine legs in the three current Graph/RV bundles, 80 matching-token tape rows were present in the artifact; all were received before the bundles were posted, so this particular smoke still correctly shows zero completed bundles. The defect is about the validity of forward completion evidence across polling/recovery windows, not a claim that the three current resting baskets should already be marked filled.

## Fresh economic context

The exact-main public smoke currently has a healthy recorder with 220 markets and 259 fresh/backfilled trades, but three three-leg `GRAPH_RV` bundles remain resting with zero completion. Runtime realized PnL and OOS PnL are both zero. The current candidates therefore do not supply positive fill/PnL evidence, but they make causal completion measurement the immediate LF bottleneck.

Forward maker calibration also remains negative/inconclusive: 32 sessions and 472 probes per policy, zero paired fills, and all join/improve/fade policies fail the paper-shadow gates. This evidence is not transferred numerically to Graph/RV, but it reinforces that fill accounting cannot be relaxed or inferred from quote edge.

External intelligence remains fail-closed: the current leading external candidates fail predictive/economic gates and there is no approved direct terminal probability to materialize as `q_external`.

## Successor contract

Before Graph/RV or Local Factor completion evidence can be used for promotion:

1. carry `received_ms` into every `TapeTrade` consumed by the multi-leg broker;
2. compare `received_ms`, not `timestamp*1000`, with order `arrival_ms` and `cancel_effective_ms`;
3. order newly observed public trades by `received_ms` before queue consumption;
4. timestamp paper fills from the local receive clock while retaining exchange timestamps as metadata;
5. combine this receive-time causality with point-in-time queue/depth/fee/target-size snapshots and same-window dependent leg states;
6. value every partial state with contemporaneous abort/unwind depth, fees and slippage at 1x/1.5x/2x costs;
7. remain fail-closed when receive-time or point-in-time execution state is unavailable.

The authorized aggressive paper envelope remains valid after these repairs; this audit does not lower statistical, execution, drawdown, kill-switch or real-money safety gates.
