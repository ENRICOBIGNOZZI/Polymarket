# Phase 8 — economic evidence and capital coordination

Status: technical readiness is separable from economic readiness; checked-in exploitation remains fail-closed.

`config/v7_economic_readiness.json` freezes the exploitation contract: at least 300 mature terminal units and 30 day blocks; complete fee, slippage, rebate, latency, unwind, and capital costs; positive 95% day-block lower bound; positive PnL under at least 2× frozen full costs; observed capital-hours, executable capacity, and drawdown; stable conditional calibration; and regime/source-health stratification. Settlement promotion additionally retains the 30-day, 2,500-contract, 300-forward-policy-trade, 30-contracts-per-calibration-bin, and uncertainty-below-edge floors.

`scripts/v7_canonical_economics.py` now maps canonical terminal units into exactly the two economic engines and emits engine/action evidence, full-cost counts including explicit rebate authority, capital-hours, frozen 1×/1.5×/2× stress, day blocks, capacity, drawdown, calibration/stratification state, and settlement evidence. It attributes each mature unit by engine, action, component provenance, market, horizon, latency regime, fill path, and cost component while continuing to read one canonical ledger only.

`scripts/v7_evidence_capital_allocator.py` consumes the declared policy. Every applicable action class must independently satisfy terminal, cost, confidence, stress, capacity, drawdown, calibration, regime, and source-health gates. The policy comparison must contain a common frozen causal observation cut for all six required benchmarks. Missing evidence produces explicit blockers and `economic_readiness=RED` even when `technical_readiness=GREEN`.

Exploration remains bounded, reserve-first, capacity-capped, concentration-capped, and separately accounted for. Exploitation output is advisory only: active PAPER envelopes are unchanged, automatic transfer is false, automatic promotion is false, and an operator-controlled promotion artifact remains required.

Gate evidence:

- `config/v7_economic_readiness.json`
- `scripts/v7_canonical_economics.py`
- `scripts/v7_evidence_capital_allocator.py`
- `tests/test_v7_canonical_economics.py`
- `tests/test_v7_evidence_capital_allocator.py`

The current repository does not claim economic readiness: the canonical benchmark block defaults to `MORE_EVIDENCE_REQUIRED` until forward exact-SHA evidence supplies a frozen causal comparison.
