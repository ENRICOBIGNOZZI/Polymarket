#!/usr/bin/env python3
"""Fail-closed repository convergence audit for the single V7 system."""
from __future__ import annotations

import argparse
import json
import re
import subprocess
from pathlib import Path
from typing import Any

from v7_strategy_governance import FAMILIES, Registry


SCHEDULER_SCHEMA = "polymarket_v7_scheduler_freeze_v1"
CAPABILITY_SCHEMA = "polymarket_v7_capability_matrix_v1"
INCUMBENT_SCHEMA = "polymarket_v7_incumbent_identity_v1"
IMPLEMENTATION_STATES = {"IMPLEMENTATION_COMPLETE", "IMPLEMENTATION_PARTIAL", "IMPLEMENTATION_MISSING"}
VALIDATION_STATES = {"ENGINEERING_VALIDATED", "FORWARD_EVIDENCE_PENDING", "ECONOMICALLY_VALIDATED", "REJECTED_BY_EVIDENCE"}
ECONOMIC_STATES = {"VALIDATED", "PROMISING_MORE_EVIDENCE_REQUIRED", "INSUFFICIENT_DATA", "REJECTED"}
REQUIRED_DOCS = {
    "ARCHITECTURE.md", "RUNTIME.md", "EXECUTION.md", "MODELS.md", "DATA.md",
    "REPLAY.md", "DEPLOYMENT.md", "MONITORING.md", "MODEL_GOVERNANCE.md", "RESEARCH.md",
}


class AuditError(ValueError):
    pass


def load(path: Path) -> dict[str, Any]:
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise AuditError(f"not_a_json_object:{path}")
    return value


def git(root: Path, *args: str) -> str:
    return subprocess.check_output(["git", "-C", str(root), *args], text=True).strip()


def top_level_schedule(text: str) -> bool:
    return re.search(r"(?m)^  schedule:\s*$", text) is not None


def validate_scheduler(root: Path) -> list[dict[str, Any]]:
    manifest = load(root / "config/v7_scheduler_freeze.json")
    if manifest.get("schema") != SCHEDULER_SCHEMA:
        raise AuditError("invalid_scheduler_schema")
    if manifest.get("paper_only") is not True or manifest.get("automatic_champion_promotion") is not False or manifest.get("automatic_deployment") is not False:
        raise AuditError("scheduler_authority_not_frozen")
    rows = manifest.get("workflows") or []
    by_id = {str(row.get("id") or ""): row for row in rows}
    actual = {path.name for path in (root / ".github/workflows").glob("*.yml")}
    if set(by_id) != actual:
        raise AuditError(f"scheduler_inventory_mismatch:missing={sorted(actual-set(by_id))}:stale={sorted(set(by_id)-actual)}")
    for name, row in by_id.items():
        text = (root / ".github/workflows" / name).read_text(encoding="utf-8")
        scheduled = top_level_schedule(text)
        if scheduled != (row.get("scheduled") is True):
            raise AuditError(f"scheduler_state_mismatch:{name}")
        mutating = any(row.get(key) is True for key in ("mutates_code", "mutates_model", "mutates_config", "mutates_runtime", "mutates_champion", "deploys"))
        if scheduled and (mutating or row.get("collects_data_only") is not True):
            raise AuditError(f"scheduled_mutating_or_unclassified_workflow:{name}")
        if row.get("decision") == "MANUAL_CUTOVER_ONLY":
            if "cutover_approved" not in text or "schedule:" in text or "workflow_run:" in text:
                raise AuditError("deployment_not_manual_cutover_only")
    return rows


def validate_capabilities(root: Path) -> dict[str, Any]:
    matrix = load(root / "config/v7_capability_matrix.json")
    if matrix.get("schema") != CAPABILITY_SCHEMA:
        raise AuditError("invalid_capability_schema")
    strategies = matrix.get("strategies") or []
    families = [str(row.get("family") or "") for row in strategies]
    if len(families) != len(set(families)) or set(families) != FAMILIES:
        raise AuditError("capability_matrix_must_cover_exactly_15_families")
    for row in [*(matrix.get("core") or []), *strategies]:
        if row.get("implementation_status") not in IMPLEMENTATION_STATES:
            raise AuditError("invalid_implementation_status")
        if row.get("validation_status") not in VALIDATION_STATES:
            raise AuditError("invalid_validation_status")
    for row in strategies:
        if row.get("economics") not in ECONOMIC_STATES:
            raise AuditError("invalid_economic_status")
        if not str(row.get("bottleneck") or "").strip():
            raise AuditError("missing_strategy_bottleneck")
    return matrix


def validate_repository(root: Path) -> dict[str, Any]:
    Registry.load(root / "config/v7_strategy_registry.json")
    directives = load(root / "config/operator_directives.json")
    auth = directives.get("paper_v7_authorization") or {}
    if auth.get("paper_only") is not True or auth.get("authenticated_execution") is not False or auth.get("real_order_submission") is not False:
        raise AuditError("operator_authority_not_paper_only")
    incumbent = load(root / "config/v7_incumbent_identity.json")
    if incumbent.get("schema") != INCUMBENT_SCHEMA or incumbent.get("paper_only") is not True:
        raise AuditError("invalid_incumbent_identity_contract")
    missing_docs = sorted(name for name in REQUIRED_DOCS if not (root / "docs" / name).is_file())
    if missing_docs:
        raise AuditError(f"canonical_docs_missing:{missing_docs}")
    bad_paths = []
    generation = re.compile(r"(^|[/_.-])v[3-6]([/_.-]|$)", re.IGNORECASE)
    for path in root.rglob("*"):
        if path.is_file() and ".git" not in path.parts and generation.search(path.relative_to(root).as_posix()):
            bad_paths.append(path.relative_to(root).as_posix())
    if bad_paths:
        raise AuditError(f"obsolete_generation_paths:{sorted(bad_paths)}")
    return incumbent


def markdown(report: dict[str, Any], matrix: dict[str, Any]) -> str:
    identity = report["identity"]
    lines = [
        "# POLYMARKET V7 FINAL ACCEPTANCE REPORT", "",
        "## Repository identity", "",
        f"- candidate SHA: `{identity['candidate_sha']}`",
        f"- main SHA: `{identity['main_sha']}`",
        f"- paper-validated SHA: `{identity['paper_validated_sha']}`",
        f"- deployed live-PAPER SHA: `{identity['deployed_sha']}`",
        "", "## Architecture", "",
        "Single V7 runtime, OMS/execution owner, physical inventory truth, capital allocator, risk owner and canonical ledger writer are enforced by repository contracts.",
        "", "## Strategy status", "",
        "| Strategy | Implementation | Validation | Economics | Main bottleneck |",
        "|---|---|---|---|---|",
    ]
    for row in matrix["strategies"]:
        lines.append(f"| {row['family']} | {row['implementation_status']} | {row['validation_status']} | {row['economics']} | {row['bottleneck']} |")
    lines.extend([
        "", "## Acceptance", "",
        f"- exact-head repository audit: `{report['valid']}`",
        f"- canonical refs equal candidate: `{report['canonical_refs_equal_candidate']}`",
        f"- deployed identity verified: `{report['deployed_identity_verified']}`",
        f"- engineering/repository acceptance: `{report['engineering_acceptance']}`",
        "- economic acceptance: strategy-specific; code existence is not alpha evidence.",
        "", "## Main bottleneck", "",
        "Maker exact-WS fillability and fill-conditioned adverse markout are the highest-information-gain incumbent experiment. External/event lanes need causal adapters and independent forward samples.",
        "", "## Next experiment", "",
        "Run the exact candidate in GREEN shadow, retain BLUE as the only writer, collect complete maker order lives on the exact WS observer, and compare queue-depletion scenarios with fills and 1/10/45/60/300s markouts.",
    ])
    return "\n".join(lines) + "\n"


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repository-root", type=Path, default=Path("."))
    parser.add_argument("--main-sha")
    parser.add_argument("--paper-validated-sha")
    parser.add_argument("--deployed-sha", default="UNKNOWN_UNVERIFIED")
    parser.add_argument("--deployed-identity-verified", action="store_true")
    parser.add_argument("--output", type=Path)
    parser.add_argument("--markdown", type=Path)
    args = parser.parse_args()
    root = args.repository_root.resolve()
    scheduler = validate_scheduler(root)
    matrix = validate_capabilities(root)
    incumbent = validate_repository(root)
    candidate = git(root, "rev-parse", "HEAD")
    main_sha = args.main_sha or git(root, "rev-parse", "origin/main")
    paper_sha = args.paper_validated_sha or git(root, "rev-parse", "origin/paper-validated")
    refs_equal = candidate == main_sha == paper_sha
    deployed_verified = bool(args.deployed_identity_verified and args.deployed_sha == candidate and incumbent.get("verified") is True)
    report = {
        "schema": "polymarket_v7_convergence_audit_v1", "valid": True,
        "identity": {"candidate_sha": candidate, "main_sha": main_sha, "paper_validated_sha": paper_sha, "deployed_sha": args.deployed_sha},
        "scheduler_count": len(scheduler), "strategy_count": len(matrix["strategies"]),
        "canonical_refs_equal_candidate": refs_equal, "deployed_identity_verified": deployed_verified,
        "engineering_acceptance": bool(refs_equal and deployed_verified),
        "paper_only": True, "automatic_promotion": False, "automatic_deployment": False,
    }
    payload = json.dumps(report, indent=2, sort_keys=True) + "\n"
    if args.output:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(payload, encoding="utf-8")
    else:
        print(payload, end="")
    if args.markdown:
        args.markdown.parent.mkdir(parents=True, exist_ok=True)
        args.markdown.write_text(markdown(report, matrix), encoding="utf-8")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
