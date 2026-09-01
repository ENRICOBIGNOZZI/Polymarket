# Phase 2 — authority and reachability audit

Status: static audit gate complete; target-topology defects explicitly carried into Phases 3–6.

The audit now has reproducible coverage for 678 tracked or intended paths, every local/remote branch and tag exposed by normal Git ref namespaces, 22 schemas, 11 workflows, 122 launcher runtime outputs, and 34 long-lived runtime processes. Each surface has exactly one classification, authority statement, replacement, validation proof, deletion gate, rollback pointer, and final disposition in `path_classification.json`.

`config/v7_authority_registry.json` establishes exactly one declared owner for coordination, capital, risk, OMS, inventory, ledger, promotion, and runtime identity. It establishes exactly two economic engines and denies all listed research families those authorities. Duplicate-owner and cross-engine component injection tests fail closed.

`config/v7_authority_edges.json` and `scripts/v7_authority_reachability_audit.py` enumerate every statically detected producer edge into the canonical ledger transport. No detected edge is unexplained. The audit intentionally does not treat explanation as remediation: it identifies component-to-ledger and dormant standalone PAPER paths as P0/P1 migration defects. In particular, standalone maker, structural, graph/RV, and micro-taker paths must be removed or forced behind the common opportunity/coordinator boundary before the target-topology gate can pass.

The active launcher remains PAPER-only with authenticated execution and real order submission disabled. Live process identities, writer counts, open orders, and PAPER positions remain an `EXTERNAL_BLOCKER` because the available PAPER-host SSH identity is not accepted. GitHub required-check administration is separately classified as an external owner action.

Gate evidence:

- `python3 scripts/v7_authority_contract.py --registry config/v7_authority_registry.json`
- `python3 scripts/v7_authority_reachability_audit.py --repository-root .`
- `python3 scripts/v7_surface_classification.py --repository-root . --validate artifacts/v7_unification/path_classification.json`
- `tests/test_v7_authority_contract.py`
- `tests/test_v7_authority_reachability_audit.py`
- `tests/test_v7_surface_classification.py`

Rollback: annotated tag `v7-unification-pre-migration-8d9e8e60` and the verified all-refs bundle recorded in `rollback_manifest.json`.
