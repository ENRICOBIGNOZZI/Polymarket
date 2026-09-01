# Phase 3 — canonical opportunity and authority boundary

Status: checked-in PAPER topology normalized; new risk remains disabled.

Both `BTC_SETTLEMENT_ENGINE` and `STRUCTURAL_ARB_ENGINE` now share one typed opportunity contract and one launched consumer, `V7_GLOBAL_PORTFOLIO_COORDINATOR`. The coordinator validates complete model/config/policy/run/snapshot identity, provenance, causal timestamps, fair-value interval, conservative wealth change, costs and authority, uncertainty, calibration, latency, capacity, exposure, settlement, replay key, and TTL. Risk `CANCEL` preempts alpha; the checked-in runtime has no option that enables new risk, so all other actionable outcomes remain `NOTHING`.

The canonical ledger router is now an authority firewall. Engine-component `CANDIDATE` and `OPPORTUNITY` records are diverted to the coordinator inbox. Research records are diverted to zero-authority evidence. Component order, fill, inventory, or PnL records without a matching coordinator receipt are quarantined and counted as rejected. Controlled exact-SHA cutover liquidation remains able to reduce and finalize pre-existing PAPER risk.

The launcher no longer starts the standalone Fast Structural PAPER executor or the disabled hard-arbitrage compatibility worker. The Fast Structural detector and External Fair shadow component retain their unique diagnostics, while their candidates converge at the same coordinator. The temporary adapter deliberately converts incomplete legacy candidates into valid `NOTHING` envelopes; it cannot manufacture missing latency, calibration, settlement, or cost authority.

Behavioral equivalence is fail-closed: the pre-migration checked-in runtime authorized no alpha actions, and the normalized runtime still authorizes no alpha actions. Deterministic tests prove cross-engine comparison, duplicate/invalid-cut rejection, risk-action priority, receipt matching, candidate diversion, research diversion, and unauthorized lifecycle quarantine.

Remaining migration work is explicit rather than reachable authority: component implementations still contain compatibility spool calls, maker and structural modeling must publish native envelopes, and the adapter is deleted only after native-envelope parity is proven. These surfaces remain classified `MERGE_INTO_CANONICAL` or `KEEP_TEMPORARY_COMPATIBILITY` with deletion gates.

Gate evidence:

- `tests/test_v7_opportunity.py`
- `tests/test_v7_global_portfolio_coordinator.py`
- `tests/test_v7_ledger_spool.py`
- `tests/test_v7_fast_authority_sentinel_contract.py`
- full CMake/CTest suite

Safety invariants: `paper_only=true`, `authenticated_execution=false`, `real_order_submission=false`, `real_capital_at_risk=false`, and no automatic promotion or capital transfer.
