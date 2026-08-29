#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Any

CANONICAL_PR_NUMBER = 740
CANONICAL_HEAD = "integration/v7-complete-20260827"
CANONICAL_BASE = "main"
INSTRUCTION_ID = "user-v7-master-multi-agent-operating-prompt-20260827"
OPERATOR_MARKER = "operator authorization change: latest explicit user instruction"
CONVERGENCE_MARKER = f"canonical convergence authority: {INSTRUCTION_ID}"
EXPECTED_CLEANUP_SEQUENCE = (
    "v7_implementation_then_tests_then_same_sha_paper_then_main_then_"
    "paper_validated_then_deploy_then_server_health_then_legacy_deletion"
)

# The canonical integration candidate inherited exactly these two operator-owned
# validator changes while repairing the V7 cutover. The special convergence
# authority does not authorize mutation of the directive itself or arbitrary
# additional authority surfaces.
OPERATOR_AUTHORITY_SURFACES = {
    "config/operator_directives.json",
    "config/project_context.json",
    "scripts/hard_safety_policy.py",
    "scripts/project_context_snapshot.py",
    "scripts/validate_project_context.py",
    "tests/test_hard_safety_policy.py",
    "tests/test_v7_authorized_paper_envelope.py",
}
AUTHORIZED_CONVERGENCE_AUTHORITY_DIFF = {
    "scripts/hard_safety_policy.py",
    "scripts/validate_project_context.py",
}


def _pr_number(event: dict[str, Any], pr: dict[str, Any]) -> int | None:
    value = event.get("number", pr.get("number"))
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _load_directives(root: Path) -> dict[str, Any]:
    value = json.loads((root / "config/operator_directives.json").read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise ValueError("config/operator_directives.json must contain an object")
    return value


def evaluate(event: dict[str, Any], changed_files: set[str], root: Path) -> tuple[list[str], dict[str, Any]]:
    errors: list[str] = []
    pr = event.get("pull_request")
    if not isinstance(pr, dict):
        return ["event does not contain pull_request metadata"], {}

    number = _pr_number(event, pr)
    head_data = pr.get("head") if isinstance(pr.get("head"), dict) else {}
    base_data = pr.get("base") if isinstance(pr.get("base"), dict) else {}
    head = str(head_data.get("ref") or pr.get("headRefName") or "")
    base = str(base_data.get("ref") or pr.get("baseRefName") or "")
    head_sha = str(head_data.get("sha") or pr.get("headRefOid") or "").lower()
    body = str(pr.get("body") or "")
    body_lower = body.lower()

    if number != CANONICAL_PR_NUMBER:
        errors.append(f"canonical convergence authority is restricted to PR #{CANONICAL_PR_NUMBER}; got {number!r}")
    if head != CANONICAL_HEAD:
        errors.append(f"canonical convergence head must be {CANONICAL_HEAD}; got {head or 'none'}")
    if base != CANONICAL_BASE:
        errors.append(f"canonical convergence base must be {CANONICAL_BASE}; got {base or 'none'}")
    if len(head_sha) != 40 or any(ch not in "0123456789abcdef" for ch in head_sha):
        errors.append("canonical convergence event must bind an exact 40-character source head SHA")
    if OPERATOR_MARKER not in body_lower:
        errors.append(f"canonical convergence PR must contain `{OPERATOR_MARKER}`")
    if CONVERGENCE_MARKER not in body_lower:
        errors.append(f"canonical convergence PR must contain `{CONVERGENCE_MARKER}`")

    changed_authority = sorted(OPERATOR_AUTHORITY_SURFACES.intersection(changed_files))
    unauthorized_authority = sorted(set(changed_authority).difference(AUTHORIZED_CONVERGENCE_AUTHORITY_DIFF))
    if unauthorized_authority:
        errors.append(
            "canonical convergence may not mutate additional operator authority surfaces: "
            + ", ".join(unauthorized_authority)
        )
    if "config/operator_directives.json" in changed_files:
        errors.append("canonical convergence must consume, not rewrite, config/operator_directives.json")

    directives = _load_directives(root)
    if directives.get("authority") != "latest_explicit_user_instruction":
        errors.append("operator authority must remain latest_explicit_user_instruction")
    if directives.get("operator_instruction_id") != INSTRUCTION_ID:
        errors.append("operator instruction id does not match the canonical convergence authorization")
    if directives.get("repository") != "ENRICOBIGNOZZI/Polymarket":
        errors.append("operator directive repository mismatch")

    architecture = directives.get("architecture") if isinstance(directives.get("architecture"), dict) else {}
    for key in ("single_execution_ledger", "single_runtime_owner", "single_broker_authority", "single_canonical_integration"):
        if architecture.get(key) is not True:
            errors.append(f"canonical V7 architecture requires {key}=true")
    if architecture.get("cleanup_sequence") != EXPECTED_CLEANUP_SEQUENCE:
        errors.append("canonical V7 cleanup sequence was changed")

    paper = directives.get("paper_v7_authorization") if isinstance(directives.get("paper_v7_authorization"), dict) else {}
    if paper.get("paper_only") is not True:
        errors.append("canonical convergence must remain PAPER-only")
    if paper.get("authenticated_execution") is not False:
        errors.append("canonical convergence must keep authenticated execution disabled")
    if float(paper.get("max_drawdown", -1.0)) != 0.15:
        errors.append("canonical convergence must preserve the authorized 15% max drawdown")

    summary = {
        "policy": "pass" if not errors else "fail",
        "canonical_pr": number,
        "canonical_head": head,
        "canonical_base": base,
        "exact_head_sha": head_sha or "none",
        "instruction_id": directives.get("operator_instruction_id", "none"),
        "operator_authority_changes": changed_authority,
        "changed_files": len(changed_files),
        "paper_only": paper.get("paper_only"),
        "authenticated_execution": paper.get("authenticated_execution"),
    }
    return errors, summary


def render(errors: list[str], summary: dict[str, Any]) -> str:
    lines = ["# Canonical V7 convergence policy", ""]
    for key in (
        "policy",
        "canonical_pr",
        "canonical_head",
        "canonical_base",
        "exact_head_sha",
        "instruction_id",
        "operator_authority_changes",
        "changed_files",
        "paper_only",
        "authenticated_execution",
    ):
        value = summary.get(key, "unknown")
        if isinstance(value, list):
            value = ", ".join(str(item) for item in value) or "none"
        lines.append(f"- {key}: `{value}`")
    if errors:
        lines.extend(["", "## Policy errors"])
        lines.extend(f"- {error}" for error in errors)
    return "\n".join(lines) + "\n"


def main() -> int:
    parser = argparse.ArgumentParser(description="Validate the one operator-authorized canonical V7 convergence PR")
    parser.add_argument("--event", required=True)
    parser.add_argument("--changed-files", required=True)
    parser.add_argument("--root", default=".")
    parser.add_argument("--output", required=True)
    args = parser.parse_args()

    try:
        event = json.loads(Path(args.event).read_text(encoding="utf-8"))
        changed = {
            line.strip()
            for line in Path(args.changed_files).read_text(encoding="utf-8").splitlines()
            if line.strip()
        }
        errors, summary = evaluate(event, changed, Path(args.root).resolve())
    except (OSError, ValueError, json.JSONDecodeError) as exc:
        errors, summary = [str(exc)], {"policy": "fail"}

    report = render(errors, summary)
    Path(args.output).write_text(report, encoding="utf-8")
    print(report, end="")
    for error in errors:
        print(f"::error::{error}")
    return 1 if errors else 0


if __name__ == "__main__":
    raise SystemExit(main())
