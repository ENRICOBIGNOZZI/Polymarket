# Phase 4 — professional maker integration

Status: maker is a zero-independent-authority component of `BTC_SETTLEMENT_ENGINE`.

The runtime does not launch the standalone PAPER maker. `v7_maker_cohort_supervisor.py` is invoked with `--observer-only` and starts only the fillability and fill-conditioned markout observers. Maker selection, quote-cell diagnostics, queue/reach estimation, conditional fill estimation, markout, latency, inventory-skew, fee/rebate, and lifecycle modeling remain preserved as BTC-engine inputs.

The BTC engine contract now fails if maker capital/OMS/inventory/ledger authority is enabled. It also requires external/oracle updates to retain cancel and reprice preemption without waiting for a Polymarket book event. Missing or immature place-ACK, cancel-ACK, taker-arrival, or private-confirmation profiles leave the engine at `CANCEL/NOTHING`; configured constants cannot impersonate empirical latency.

Maker, taker, and no-action proposals share the conservative account-wealth objective at the global coordinator. The ledger authority firewall diverts maker candidates to the coordinator and quarantines maker lifecycle events without a matching receipt. Since checked-in new-risk authorization is false, the migration preserves existing behavior while eliminating the standalone economic authority surface.

Gate evidence:

- `config/v7_btc_settlement_engine.json`
- `scripts/v7_btc_settlement_engine_contract.py`
- `tests/test_v7_btc_settlement_engine_contract.py`
- `tests/test_v7_maker_cohort_supervisor.py`
- `tests/test_v7_market_maker_runtime_contract.py`
- `tests/test_v7_ledger_spool.py`

Remaining maker C++ lifecycle code is retained only as unlaunched compatibility/modeling code until native-envelope parity and deletion proofs are complete.
