# Runtime action report and hedge coherence

The paper runtime must explain the complete decision funnel rather than report only the final PnL:

`raw signal -> executable edge -> coherent hedge -> broker admission -> queue/fill evidence -> close or unwind -> realized net PnL`.

`scripts/runtime_action_report.py` writes atomic JSON and Markdown reports describing what each sleeve observed, what the system did, and why it acted, waited, blocked, or abstained. The report is diagnostic only. It cannot relax thresholds, manufacture fills, enable authenticated execution, or book estimated rewards as realized PnL.

B2 factor candidates are filtered before intent construction. `scripts/filter_coherent_hedges.py` preserves raw, accepted, and rejected files separately and rejects baskets whose hedge legs do not belong to an economically defensible event or semantic cluster. A candidate that depends on unrelated cross-domain legs is not alpha and must fail closed.

The durable accounting source remains `bundle_ledger.csv`. `CLOSED` and `UNWOUND` outcomes both enter evaluation. Grafana and the runtime action report are operational views and must reconcile with persisted state rather than replace it.

Any future Alpha Factory promotion must consume these diagnostics, retain the raw rejection evidence, and pass the existing OOS, cost-stress, drawdown, execution, and paper-only gates before it can become the single paper champion.
