# V7 execution-evidence research note

## Decision under test

V6 currently combines several economically distinct activities: short-horizon market making/taking, relative-value convergence, deterministic payout constraints, and externally informed terminal-probability estimates. A single aggregate PnL or win-rate is not valid evidence for all of them.

The V7 question is therefore deliberately narrow:

> Can the paper runtime produce target-specific, execution-aware evidence that is sufficient to **evaluate** a model without changing its allocation, risk limits, credentials, order submission, or live-money status?

This is instrumentation research, not an alpha claim. A lack of observations is a failed gate, never a positive result.

## Economic target contracts

| Target | Valid primary evidence | Invalid substitute |
| --- | --- | --- |
| `micro_maker`, `micro_taker` | short-horizon markout after an actual fill, plus realized post-cost PnL | quoted edge, submitted order, or an entry-side zero-PnL row |
| `relative_value` | hedged convergence and bundle-level realized PnL | unhedged leg PnL or directional markout |
| `graph_hard` | structural payout / resolution consistency after complete execution | predictive calibration or generic trade win rate |
| `external` | terminal probability calibration against the contemporaneous market probability on resolved outcomes | unrealized PnL, proxy labels, or a global calibration score |

Evidence must never be pooled across those target types.

## Falsifiable V7 protocol

For each model/expert/target stream, the proposed sidecar must:

1. Count actual fills independently of submissions; a missing denominator reports `null`, not a fabricated fill rate.
2. Treat PnL as realized only for ledger rows or exit/unwind/sell/settlement actions; entry-side zeros are excluded.
3. Apply a 1.5× fee/slippage stress to realized economics.
4. Estimate the probability that mean daily PnL is non-positive with deterministic day-block bootstrap resampling.
5. Require two non-overlapping positive time blocks rather than one aggregate period.
6. For terminal-probability targets, require resolved labels plus both model and market probabilities, and require non-negative Brier improvement over the market baseline.
7. Emit explicit `INSUFFICIENT_EVIDENCE` or `REJECTED` states where fields, samples, freshness, or gates are absent.

The initial conservative sample thresholds are documented in `config/v7_execution_evidence.json`; they are acceptance gates, not performance estimates.

## Safety and integration boundary

The candidate may:

- read existing paper-runtime CSV/JSON artifacts;
- write an atomic evidence JSON/Markdown report;
- expose read-only Prometheus and Grafana diagnostics.

It may not:

- alter capital allocation or a champion manifest;
- alter entry thresholds, risk limits, sizing, or submission paths;
- enable real-money execution;
- infer success from no-fill, no-PnL, or missing-label conditions.

Any later promotion of an individual model remains a separate evidence and governance decision.

## Pre-integration review result

The proposed implementation satisfies the above contract in focused tests:

- target contracts reject cross-target mixing;
- terminal calibration without resolved labels fails closed;
- a synthetic two-day paper ledger can become eligible only for its own target;
- empty runtime data remains non-eligible and cannot reallocate capital;
- report writes are atomic.

The change is ready to integrate as a paper-only diagnostic sidecar. It does **not** certify any V6 strategy as profitable or ready for live execution.
