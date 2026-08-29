# V4 research ledger and OOS interpretation

The purpose of V4 is to answer a narrow question: **does a quoted statistical edge survive realistic execution?**

The causal sequence recorded by the system is

`signal → bundle intent → passive orders → public trade prints → partial fills → completion/unwind → realized net PnL`.

The ledger must therefore be interpreted differently from a conventional price-bar backtest. A raw B1/B2 dislocation is not a trade. A maker-entry estimate is not a fill. A partial basket is not a completed hedge. `UNWOUND` is an economic outcome and is included in evaluation.

`walk_forward_v4.py` excludes non-final live bundle states, but includes both `CLOSED` and `UNWOUND`. This means leg-risk failures and execution timeouts cannot disappear from historical performance merely because the intended convergence model was correct.

The pilot gate is intentionally strict. A report with `eligible_for_tiny_pilot=false` is a valid result and must not be bypassed by lowering thresholds after observing the OOS segment.
