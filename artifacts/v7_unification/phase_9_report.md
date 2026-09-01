# Phase 9 — exact-SHA PAPER validation and cutover evidence

```text
PHASE: 9
BASE_SHA: 8d9e8e603aae4d73842212eedcf8e0e06383127f
HEAD_SHA: 3c32a60d7ca3b5d3d97ae8b74a80a04c0978c2d4
FILES_CHANGED: .github/workflows/v7-live-paper-validation.yml; scripts/verify_v7.sh; scripts/v7_surface_classification.py; tests/test_v7_live_paper_validation_contract.py; tests/test_v7_verify_contract.py; tests/test_v7_surface_classification.py; artifacts/v7_unification/path_classification.json; artifacts/v7_unification/phase_9_canary_attestation.json; artifacts/v7_unification/phase_9_report.md
INVARIANTS_PROVEN: prerequisite-blocked validation fails instead of skipped-success; exact-SHA Release/Debug/sanitizer/security green; clean-checkout ref classification reproducible; one 33-process resolved manifest; two engines; new risk disabled; authenticated execution/real orders/real capital disabled
TESTS_RUN: canonical verify_v7 with ASan+UBSan; current-tree and full-history pattern/entropy scans; Release and Debug 221-case CTest matrices at corrected tree; clean single-branch clone classification; 121-second bounded public-data PAPER canary; cutover/chaos/replay/rollback tests included in the 221-case matrices
TEST_RESULTS: PASS locally and GitHub Actions run 33498896619; canary PASS
RUNTIME_EVIDENCE: exact SHA, config/policy/model hashes, run ID, ledger ID, server ID, fresh runtime status, exact CI receipt, controlled shutdown, and resolved 33-process manifest recorded in phase_9_canary_attestation.json; full external-fair readiness remained false and the runtime stayed CORE_RUNTIME_ONLY/CANCEL_NOTHING_ONLY
ECONOMIC_EVIDENCE: MORE_EVIDENCE_REQUIRED; promotion_ready=false; submitted_units=0; complete_units=0; no profitability claim
BLOCKERS: PAPER host SSH authentication unavailable; GitHub admin authentication unavailable; main-only monitoring/private-single-writer/live-PAPER workflows cannot be dispatched with current access, so server blue/green rehearsal and branch-protection proof remain external
ROLLBACK_POINTER: v7-unification-pre-migration-8d9e8e60; /Users/enrico/polymarket-backups/v7-unification-8d9e8e60/polymarket-all-refs.bundle (sha256 6b1fe45289cbb0c14aff6e31960851d197e87f6fe0534d56518f73f9509e6eee)
NEXT_PHASE: execute the repository-side deletion manifest in small equivalence-proven waves while preserving rollback evidence
```

The canary intentionally did not claim full-stack or economic readiness. Public sources did not reach the full external-fair readiness contract during the bounded interval, so the runtime remained fail-closed with only `CANCEL` and `NOTHING` authorized. This is the expected safe result.

The deployment workflow's blue/green ordering and its failure cases are executable-test covered, including flat and nonflat incumbents, partial startup, never-started components, stale state, archive interruption, canonical absence proof, and rollback-pointer preservation. A physical PAPER-host rehearsal cannot be represented as completed until SSH owner access is restored.
