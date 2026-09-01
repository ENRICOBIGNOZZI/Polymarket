#!/usr/bin/env python3
"""Fail closed unless the checked-out tree matches the final unified V7 contract."""
from __future__ import annotations

import argparse
import json
import subprocess
from collections import Counter
from pathlib import Path
from typing import Any

from v7_authority_reachability_audit import audit as audit_authority
from v7_process_manifest import resolve as resolve_process_manifest
from v7_surface_classification import build_manifest, validate_manifest


SCHEMA = "polymarket_v7_final_convergence_audit_v1"
EXPECTED_ENGINES = {"CRYPTO_SETTLEMENT_ENGINE", "STRUCTURAL_ARB_ENGINE"}
EXPECTED_OWNERS = {
    "global_portfolio_coordinator",
    "capital_allocator",
    "risk_engine",
    "oms",
    "inventory",
    "ledger",
    "promotion",
    "runtime_identity",
}
REQUIRED_CHECKS = {
    "ci-v7-Release",
    "ci-v7-Debug",
    "security-audit-v7",
    "sanitizer-v7",
    "monitoring-v7",
    "single-writer-v7",
}
SBOM_PACKAGES = {"polymarket-v7", "Boost", "OpenSSL", "libcurl", "Python"}


class FinalConvergenceError(ValueError):
    pass


def load(path: Path) -> dict[str, Any]:
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise FinalConvergenceError(f"not_an_object:{path}")
    return value


def require_safety(name: str, value: dict[str, Any]) -> None:
    if (
        value.get("paper_only") is not True
        or value.get("authenticated_execution") is not False
        or value.get("real_order_submission") is not False
        or value.get("real_capital_at_risk") is not False
    ):
        raise FinalConvergenceError(f"unsafe_contract:{name}")


def validate_ruleset(value: dict[str, Any]) -> list[str]:
    if value.get("target") != "branch" or value.get("enforcement") != "active":
        raise FinalConvergenceError("ruleset_identity")
    rules = value.get("rules")
    if not isinstance(rules, list):
        raise FinalConvergenceError("ruleset_rows")
    by_type = {row.get("type"): row for row in rules if isinstance(row, dict)}
    if not {"deletion", "non_fast_forward", "required_linear_history"} <= set(by_type):
        raise FinalConvergenceError("ruleset_history_protection")
    review = by_type.get("pull_request", {}).get("parameters", {})
    if (
        review.get("require_code_owner_review") is not True
        or review.get("require_last_push_approval") is not True
        or review.get("required_review_thread_resolution") is not True
        or review.get("required_approving_review_count") != 1
    ):
        raise FinalConvergenceError("ruleset_authority_review")
    checks = by_type.get("required_status_checks", {}).get("parameters", {}).get(
        "required_status_checks", []
    )
    actual = {
        row.get("context") for row in checks if isinstance(row, dict)
    }
    if actual != REQUIRED_CHECKS:
        raise FinalConvergenceError(f"ruleset_required_checks:{sorted(actual)}")
    return sorted(actual)


def validate_sbom(value: dict[str, Any]) -> list[str]:
    if value.get("spdxVersion") != "SPDX-2.3":
        raise FinalConvergenceError("sbom_version")
    packages = value.get("packages")
    if not isinstance(packages, list):
        raise FinalConvergenceError("sbom_packages")
    names = {row.get("name") for row in packages if isinstance(row, dict)}
    if names != SBOM_PACKAGES:
        raise FinalConvergenceError(f"sbom_coverage:{sorted(str(name) for name in names)}")
    return sorted(str(name) for name in names)


def build(root: Path) -> dict[str, Any]:
    root = root.resolve()
    registry = load(root / "config/v7_authority_registry.json")
    edges = load(root / "config/v7_authority_edges.json")
    process_contract = load(root / "config/v7_process_manifest.json")
    readiness = load(root / "config/v7_economic_readiness.json")
    directives = load(root / "config/operator_directives.json")
    require_safety("authority_registry", registry)
    require_safety("process_manifest", process_contract)
    require_safety("economic_readiness", readiness)
    require_safety("operator_directives", directives["paper_v7_authorization"])

    authority = audit_authority(root, registry, edges)
    if (
        set(authority["economic_engines"]) != EXPECTED_ENGINES
        or set(authority["owner_counts"]) != EXPECTED_OWNERS
        or any(count != 1 for count in authority["owner_counts"].values())
        or authority["known_migration_defect_count"] != 0
        or authority["unexplained_edges"]
        or authority["audit_gate"]["target_topology_complete"] is not True
    ):
        raise FinalConvergenceError("authority_topology")

    process = resolve_process_manifest(root, process_contract)
    expected_process_authorities = {key: 1 for key in EXPECTED_OWNERS}
    expected_process_authorities["promotion"] = 0
    if (
        process["process_count"] != 33
        or process["launcher_child_count"] != 31
        or process["launcher_manifest_parity"] is not True
        or process["research_fault_isolation"] is not True
        or process["authority_counts"] != expected_process_authorities
    ):
        raise FinalConvergenceError("runtime_process_topology")

    surface = build_manifest(root)
    surface_validation = validate_manifest(surface, root=root)
    classifications = Counter(row["classification"] for row in surface["entries"])
    if classifications["DELETE_ACTIVE_LEGACY"] or classifications["KEEP_TEMPORARY_COMPATIBILITY"]:
        raise FinalConvergenceError("legacy_or_temporary_surface_remains")

    checks = validate_ruleset(load(root / "artifacts/github_main_ruleset.json"))
    sbom_packages = validate_sbom(load(root / "artifacts/v7_sbom.spdx.json"))
    head = subprocess.check_output(
        ["git", "-C", str(root), "rev-parse", "HEAD"], text=True
    ).strip()
    blockers = sorted(
        row["surface_id"] for row in surface["entries"]
        if row["classification"] == "EXTERNAL_BLOCKER"
    )
    return {
        "schema": SCHEMA,
        "valid": True,
        "source_snapshot_sha": head,
        "baseline_sha": "8d9e8e603aae4d73842212eedcf8e0e06383127f",
        "safety": {
            "paper_only": True,
            "authenticated_execution": False,
            "real_order_submission": False,
            "real_capital_at_risk": False,
            "automatic_capital_transfer": False,
            "automatic_promotion": False,
        },
        "architecture": {
            "system_count": 1,
            "economic_engine_count": 2,
            "economic_engines": sorted(EXPECTED_ENGINES),
            "owner_counts": authority["owner_counts"],
            "decision_chain": authority["decision_chain"],
            "known_migration_defect_count": 0,
            "static_ledger_transport_edge_count": len(authority["static_ledger_transport_edges"]),
        },
        "runtime_processes": {
            "declared_count": process["process_count"],
            "launcher_child_count": process["launcher_child_count"],
            "launcher_manifest_parity": True,
            "research_fault_isolation": True,
            "authority_counts": process["authority_counts"],
        },
        "surfaces": {
            "entry_count": surface_validation["entry_count"],
            "classification_counts": dict(sorted(classifications.items())),
            "delete_active_legacy_count": 0,
            "temporary_compatibility_count": 0,
        },
        "governance": {
            "required_checks": checks,
            "code_owner_review_required": True,
            "ruleset_manifest_ready": True,
            "ruleset_applied": False,
            "state": "EXTERNAL_BLOCKER",
        },
        "sbom": {
            "format": "SPDX-2.3",
            "path": "artifacts/v7_sbom.spdx.json",
            "packages": sbom_packages,
        },
        "readiness": {
            "technical_repository_state": "VALIDATED_BY_THIS_AUDIT",
            "physical_paper_runtime_state": "EXTERNAL_BLOCKER",
            "economic_state": "MORE_EVIDENCE_REQUIRED",
            "promotion_ready": False,
            "profitability_proven": False,
        },
        "external_blockers": blockers,
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repository-root", type=Path, default=Path("."))
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()
    try:
        result = build(args.repository_root)
        rendered = json.dumps(result, indent=2, sort_keys=True) + "\n"
        if args.output:
            args.output.parent.mkdir(parents=True, exist_ok=True)
            args.output.write_text(rendered, encoding="utf-8")
        else:
            print(rendered, end="")
        return 0
    except (
        OSError,
        KeyError,
        json.JSONDecodeError,
        subprocess.CalledProcessError,
        ValueError,
    ) as exc:
        print(f"v7_final_convergence_audit: {exc}", file=__import__("sys").stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
