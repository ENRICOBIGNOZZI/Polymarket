# Phase 5 — structural arbitrage unification

Status: one structural owner, one proposal lifecycle, no independent execution authority.

`STRUCTURAL_ARB_ENGINE` now has its own canonical contract in `config/v7_structural_arb_engine.json`; it is no longer embedded in the BTC engine configuration. The contract requires full-depth books, direct joint completion, one atomic economic intent, one capital reservation, partial-fill and timeout plans, and a full-depth-bounded unwind plan. Capital, risk, OMS, inventory, and ledger remain the shared canonical owners.

The common opportunity envelope now carries an explicit atomic execution plan with stable unit ID, ordered legs, per-leg instrument/side/quantity/limit/fee authority, partial-fill policy, timeout, and unwind policy. An `ARB` envelope with fewer than two legs or without complete-or-unwind semantics fails closed.

The launcher starts the Fast Structural detector but no longer starts the independent Fast Structural PAPER executor or hard-arbitrage guard. Detector bundles reach the coordinator as one envelope through the temporary compatibility adapter; the existing `structured_legs` payload is preserved. Incomplete legacy evidence is forced to `NOTHING`, never promoted into executable economics. Near-miss and opportunity-loss evidence remains observational.

No structural component can double-reserve capital or write orders/fills/PnL directly: candidates are diverted to the single coordinator, while unreceipted lifecycle records are quarantined by the ledger firewall. With new risk disabled, there is no active structural position lifecycle to reconcile or terminal PnL unit to split.

Gate evidence:

- `config/v7_structural_arb_engine.json`
- `schemas/v7/opportunity_envelope.schema.json`
- `scripts/v7_opportunity.py`
- `scripts/v7_global_portfolio_coordinator.py`
- `tests/test_v7_opportunity.py`
- `tests/test_v7_global_portfolio_coordinator.py`
- `tests/test_v7_fast_authority_sentinel_contract.py`

The unlaunched compatibility executor and guard remain deletion-gated until their unique feasibility/unwind tests are migrated into the engine boundary.
