# V7 execution evidence

V7 includes a read-only, paper-only evidence research utility. It exists to answer a
specific question for every sleeve: is its claimed economic object supported by
realized, executable observations?

It does not submit, cancel, modify, or size orders. It cannot change capital
allocation, risk limits, credentials, or authenticated execution.

## Target contracts

| Sleeve | Economic target | Required evidence |
|---|---|---|
| Micro maker / taker | short-horizon markout | fills, forward markout, realized PnL, cost stress |
| Relative value | hedged convergence | completed basket PnL, fill/completion rate, forward markout, cost stress |
| Graph hard | structural payout | completed/settled basket PnL and cost stress |
| External | terminal probability | terminal calibration plus executable PnL and cost stress |

Terminal probabilities are never blended into a short-horizon maker or
relative-value target. A missing observation is not converted into a zero or a
positive result: it is a failed evidence gate.

## Output

Each report is written atomically under the live run root:

```text
v7_execution_evidence.json
v7_execution_evidence.md
```

The report includes source file provenance, fill counts, realized PnL
observations, forward-markout observations, 1.5x cost stress, two-fold time
stability, and a deterministic day-block bootstrap p-value. Models are labelled
`PAPER_ELIGIBLE` only after all configured gates pass; otherwise they remain
`INSUFFICIENT_EVIDENCE` with explicit reason codes.

The utility is intentionally fail-closed and V7-only. The canonical 24/7
runtime obtains operational truth from the append-only V7 ledger and
`canonical_economics.json`; this utility does not own a required runtime
surface, reallocate capital, or gate PAPER activity. A future allocator may
consume only typed canonical evidence and remains a separate economic change.
