# Phase 6 — zero-authority research and external data plane

Status: retained information sources are explicitly separated from economic authority.

`config/v7_research_data_plane.json` documents the unique purpose, retention basis, process set, output set, feature schema, provenance, causality/source-health/tape posture, market/entity/settlement mapping status, four required ablations, credential class, and authority flags for all ten research families. Capital, OMS, inventory, ledger, order, and promotion authority are false for every family. Trading tokens are forbidden; an authenticated read-only collector must use a separate non-trading credential.

`scripts/v7_research_data_plane_contract.py` cross-checks the research families against the canonical authority registry and live scope, verifies every declared process exists, and rejects any research source that imports or calls the ledger spool or constructs canonical ledger events. Partial mapping coverage is reported honestly for sports latency and cross-platform sources; it cannot be presented as complete evidence.

Three P0 research bypasses were removed:

- the dormant standalone Graph/RV PAPER broker was deleted after its causal-book coverage was moved into the retained scanner tests and its direct joint-completion coverage was confirmed in the structural engine tests;
- Graph/RV cost reconstruction now reads historical ledger evidence into `research/evidence/graph_cost_vector.json` and cannot annotate the canonical ledger;
- Micro Taker is intrinsically research-only. It has no mode flag that can restore PAPER behavior, refuses state containing prior positions, creates no inventory state, and has no ledger/order/capital/OMS/promotion imports or writes.

The authority audit consequently fell from nine to six known migration defects, with no unexplained ledger-transport edge.

Gate evidence:

- `config/v7_research_data_plane.json`
- `scripts/v7_research_data_plane_contract.py`
- `tests/test_v7_research_data_plane_contract.py`
- `tests/test_v7_micro_workers.py`
- `tests/test_v7_structural_rotation.py`
- `artifacts/v7_unification/authority_graph.json`

Remaining mapping uncertainty is an explicit research-quality blocker, not execution authority.
