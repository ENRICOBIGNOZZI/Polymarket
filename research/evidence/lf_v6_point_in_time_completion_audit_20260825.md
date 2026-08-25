# LF V6 point-in-time completion audit

Decision: **MORE_EVIDENCE_REQUIRED**

Scope: research-only audit of multi-leg Graph/RV completion and fill-conditioned PnL. No production model, champion, risk, sizing, deployment, credential, or authenticated-execution path is changed.

## Finding

The frontier completion guard in PR #332 improves on an independence product by estimating same-window joint fill states from the public trade tape. However, it labels every historical tape window with one **current** CLOB snapshot:

- current best bid and current bid size are fetched once per leg;
- required compatible volume is `current_queue_ahead + current_target_shares`;
- the same requirement is applied to all historical windows;
- partial-fill unwind loss for all historical windows uses the current bid, adjusted only by a fixed slippage stress.

That is not a point-in-time replay. Historical completion probability and historical abort/unwind PnL are not identified by a trade tape plus one current book snapshot.

## Deterministic counterexample

Take two historical windows with the same compatible sell volume (50 shares) and the same 10-share target:

| Window | Historical queue ahead | Historical unwind bid | True fill |
|---|---:|---:|---:|
| A | 90 | 0.20 | no |
| B | 10 | 0.39 | yes |

Both have entry price 0.40. The true point-in-time completion rate is 50%.

If the **current** queue is 10 shares, replaying both old windows against that current state marks both as filled: estimated completion becomes 100%. If the current queue is 90 shares, the same historical tape marks both as unfilled: estimated completion becomes 0%.

With a +$1 completed-bundle profit, the point-in-time state EV in this toy example is -$0.50 per window: window A loses $2 on a 10-share unwind from 0.40 to 0.20, while window B earns $1. Reusing a current queue of 10 and current bid of 0.39 labels both windows complete and reports +$1 mean EV. Thus the current-state replay can flip the sign of fill-conditioned EV without changing the historical tape.

This is an identification counterexample, not an estimate of live Polymarket PnL.

## Required repair before promotion

1. Persist point-in-time queue/depth, executable quote, target size, fee state, and timestamp at each candidate/window origin.
2. Persist or reconstruct point-in-time unwind bid/depth and fees at each abort horizon.
3. Estimate joint completion from common historical states only when all required point-in-time state is available; otherwise use forward-only shadow observations.
4. Keep the state-vector approach for dependence and partial-fill subsets, but never label old windows with the current order book.
5. Evaluate completion rate, partial-state frequencies, abort/unwind loss, and fill-conditioned PnL on chronological forward evidence at 1x/1.5x/2x costs.

## Current public context

The latest exact-main public V6 smoke has two three-leg Graph/RV bundles resting, `complete=0`, 218 public trades processed in the current tape, 0 OOS trades, and $0 realized/OOS PnL. The maker path also has seven resting orders but zero positions/fills. The new V7 typed execution-evidence sidecar is fail-closed and useful for realized evidence aggregation, but it does not reconstruct historical order-book state. External Intelligence remains BACKTESTING with no passing model. Therefore there is no current fill/PnL evidence that justifies promotion of the frontier completion economics.
