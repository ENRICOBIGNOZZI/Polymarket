# V7 claim policy

Claims are exact-SHA statements, never descriptions of an implementation.
Their machine-readable requirements are in
[`config/v7_claim_registry.json`](../../config/v7_claim_registry.json).

- `PAPER_SIMULATED`, replay, telemetry, quoted edge, counterfactual fills, and
  mark PnL cannot satisfy `REAL_PNL_VERIFIED`.
- A real-PnL claim requires terminal real evidence, independent reconciliation,
  zero unresolved breaks, and a signed immutable attestation for the exact
  code/config/model/run identity.
- `WORLD_CLASS_CANDIDATE` additionally requires real PnL, a positive
  conservative lower confidence bound, capacity, controlled tail risk,
  multi-regime evidence, recovery/security evidence, and an external verifier.
- If any required evidence is absent, stale, mixed-SHA, synthetic, or
  unreconciled, the claim is false or unknown. The correct active state is
  `MORE_EVIDENCE_REQUIRED` or `PROFITABILITY_NOT_TESTABLE`.

No process may automatically promote PAPER to live or increase a live cap.
Checked-in caps are always zero, and no private authorization document is part
of the V7 control manifest.

Mutable `latest` reports are non-authoritative. Store report bytes with
[`scripts/v7_artifact_store.py`](../../scripts/v7_artifact_store.py) under
`artifacts/by_sha/<sha>/<run_id>/`; a path collision with different bytes fails.
