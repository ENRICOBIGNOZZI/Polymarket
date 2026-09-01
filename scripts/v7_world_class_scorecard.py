#!/usr/bin/env python3
"""Validate the aggregate V7 world-class status without promoting anything.

The economic calculator remains in :mod:`v7_real_pnl_scorecard`; this module
defines the status-artifact contract used to publish its result alongside
execution, accounting, reliability, and security evidence. It cannot create
an eligible result, submit an order, or promote a model.
"""
from __future__ import annotations

import json
import re
import subprocess
import sys
from pathlib import Path
from typing import Any

from v7_real_pnl_scorecard import main as economic_main
import v7_security_audit as security_audit


SCHEMA = "polymarket_v7_world_class_status_v1"
SHA_RE = re.compile(r"^[0-9a-f]{40}$")
SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
STATES = {"MORE_EVIDENCE_REQUIRED", "REAL_PNL_ECONOMIC_PROOF", "WORLD_CLASS_CANDIDATE"}


class WorldClassScorecardError(ValueError):
    pass


def _optional_number(value: Any, field: str) -> None:
    if value is not None and (isinstance(value, bool) or not isinstance(value, (int, float))):
        raise WorldClassScorecardError(f"{field}:invalid")


def _canonical_bytes(value: Any) -> bytes:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False, allow_nan=False).encode("utf-8")


def _sha256(value: Any) -> str:
    import hashlib
    return hashlib.sha256(_canonical_bytes(value)).hexdigest()


def current_status(root: Path) -> dict[str, Any]:
    """Build the honest scorecard from a checkout, never from PAPER outcomes."""
    root = Path(root).resolve()
    try:
        model_sha = subprocess.check_output(["git", "-C", str(root), "rev-parse", "HEAD"], text=True).strip()
        worktree_dirty = bool(subprocess.check_output(
            ["git", "-C", str(root), "status", "--porcelain", "--untracked-files=no"], text=True
        ).strip())
    except (OSError, subprocess.CalledProcessError) as exc:
        raise WorldClassScorecardError("repository:head_unavailable") from exc
    if not SHA_RE.fullmatch(model_sha):
        raise WorldClassScorecardError("repository:head_invalid")
    try:
        audit = security_audit.audit(root)
    except ValueError as exc:
        raise WorldClassScorecardError(f"security_audit:{exc}") from exc
    reasons = [
        "no_authenticated_read_only_reconciliation",
        "no_real_terminal_economic_units",
        "no_signed_real_pnl_attestation",
        "no_forward_capacity_or_latency_evidence",
    ]
    if audit["state"] != "MORE_EVIDENCE_REQUIRED":
        reasons.insert(0, "security_controls_not_ready")
    if worktree_dirty:
        reasons.insert(0, "working_tree_dirty")
    value = {
        "schema": SCHEMA, "model_sha": model_sha, "state": "MORE_EVIDENCE_REQUIRED",
        "world_class_candidate": False, "automatic_promotion": False, "reason_codes": reasons,
        "economics": {"realized_and_settled_net_pnl_base_units": None, "conservative_net_pnl_base_units": None,
                      "lower_confidence_bound_base_units": None},
        "execution": {"order_ack_latency": None, "cancel_ack_latency": None, "fill_calibration": None},
        "accounting": {"unresolved_reconciliation_breaks": None, "independent_verifier_reproducible": False},
        "reliability": {"chaos_test_pass_rate": None, "production_recovery_verified": False},
        "security": {"secret_scan_clean": audit["inputs"]["pattern_secret_scan"]["finding_count"] == 0
                                           and audit["inputs"]["entropy_secret_scan"]["finding_count"] == 0,
                     "private_release_governance_verified": False},
        "evidence": {"real_pnl_scorecard_sha256": None, "execution_evidence_sha256": None,
                     "data_research_audit_sha256": None, "reliability_evidence_sha256": None,
                     "security_audit_sha256": _sha256(audit), "benchmark_methodology_sha256": None},
    }
    return validate_status(value)


def validate_status(value: Any) -> dict[str, Any]:
    """Validate a published status and enforce its non-promotional invariant."""
    required = {"schema", "model_sha", "state", "world_class_candidate", "automatic_promotion",
                "reason_codes", "economics", "execution", "accounting", "reliability", "security", "evidence"}
    if not isinstance(value, dict) or set(value) != required:
        raise WorldClassScorecardError("scorecard:shape")
    if value["schema"] != SCHEMA or not isinstance(value["model_sha"], str) or not SHA_RE.fullmatch(value["model_sha"]):
        raise WorldClassScorecardError("scorecard:identity")
    if value["state"] not in STATES or not isinstance(value["world_class_candidate"], bool):
        raise WorldClassScorecardError("scorecard:state")
    if value["automatic_promotion"] is not False:
        raise WorldClassScorecardError("scorecard:automatic_promotion_forbidden")
    if not isinstance(value["reason_codes"], list) or any(not isinstance(item, str) or not item for item in value["reason_codes"]):
        raise WorldClassScorecardError("scorecard:reason_codes")
    candidate = value["world_class_candidate"]
    if candidate != (value["state"] == "WORLD_CLASS_CANDIDATE"):
        raise WorldClassScorecardError("scorecard:candidate_state_mismatch")
    if candidate and value["reason_codes"]:
        raise WorldClassScorecardError("scorecard:candidate_has_unresolved_reasons")

    economics = value["economics"]
    if not isinstance(economics, dict) or set(economics) != {
            "realized_and_settled_net_pnl_base_units", "conservative_net_pnl_base_units",
            "lower_confidence_bound_base_units"}:
        raise WorldClassScorecardError("scorecard:economics")
    for field, item in economics.items():
        _optional_number(item, f"scorecard:economics:{field}")
    execution = value["execution"]
    if not isinstance(execution, dict) or set(execution) != {"order_ack_latency", "cancel_ack_latency", "fill_calibration"}:
        raise WorldClassScorecardError("scorecard:execution")
    for field, item in execution.items():
        _optional_number(item, f"scorecard:execution:{field}")
    accounting = value["accounting"]
    if (not isinstance(accounting, dict) or set(accounting) !=
            {"unresolved_reconciliation_breaks", "independent_verifier_reproducible"}
            or (accounting["unresolved_reconciliation_breaks"] is not None
                and (isinstance(accounting["unresolved_reconciliation_breaks"], bool)
                     or not isinstance(accounting["unresolved_reconciliation_breaks"], int)
                     or accounting["unresolved_reconciliation_breaks"] < 0))
            or not isinstance(accounting["independent_verifier_reproducible"], bool)):
        raise WorldClassScorecardError("scorecard:accounting")
    reliability = value["reliability"]
    if (not isinstance(reliability, dict) or set(reliability) !=
            {"chaos_test_pass_rate", "production_recovery_verified"}
            or not isinstance(reliability["production_recovery_verified"], bool)):
        raise WorldClassScorecardError("scorecard:reliability")
    _optional_number(reliability["chaos_test_pass_rate"], "scorecard:reliability:chaos_test_pass_rate")
    security = value["security"]
    if (not isinstance(security, dict) or set(security) !=
            {"secret_scan_clean", "private_release_governance_verified"}
            or any(not isinstance(security[field], bool) for field in security)):
        raise WorldClassScorecardError("scorecard:security")
    evidence = value["evidence"]
    evidence_fields = {
        "real_pnl_scorecard_sha256", "execution_evidence_sha256", "data_research_audit_sha256",
        "reliability_evidence_sha256", "security_audit_sha256", "benchmark_methodology_sha256",
    }
    if (not isinstance(evidence, dict) or set(evidence) != evidence_fields
            or any(item is not None and (not isinstance(item, str) or not SHA256_RE.fullmatch(item))
                   for item in evidence.values())):
        raise WorldClassScorecardError("scorecard:evidence")
    if candidate and not all((
            all(item is not None and item > 0 for item in economics.values()),
            all(item is not None for item in execution.values()),
            accounting["unresolved_reconciliation_breaks"] == 0,
            accounting["independent_verifier_reproducible"],
            reliability["chaos_test_pass_rate"] is not None,
            reliability["production_recovery_verified"],
            security["secret_scan_clean"],
            security["private_release_governance_verified"],
            all(item is not None for item in evidence.values()),
    )):
        raise WorldClassScorecardError("scorecard:candidate_evidence_incomplete")
    return value


def _validate_main(argv: list[str]) -> int:
    if len(argv) != 2:
        raise WorldClassScorecardError("usage: validate STATUS_JSON")
    value = validate_status(json.loads(Path(argv[1]).read_text(encoding="utf-8")))
    print(json.dumps({"valid": True, "state": value["state"], "world_class_candidate": value["world_class_candidate"]}, sort_keys=True))
    return 0


def _current_main(argv: list[str]) -> int:
    if len(argv) not in {2, 4} or argv[0] != "current":
        raise WorldClassScorecardError("usage: current ROOT [--output PATH]")
    value = current_status(Path(argv[1]))
    if len(argv) == 4:
        if argv[2] != "--output":
            raise WorldClassScorecardError("usage: current ROOT [--output PATH]")
        output = Path(argv[3])
        output.parent.mkdir(parents=True, exist_ok=True)
        output.write_text(json.dumps(value, sort_keys=True, indent=2) + "\n", encoding="utf-8")
    else:
        print(json.dumps(value, sort_keys=True))
    return 0


if __name__ == "__main__":
    if len(sys.argv) > 1 and sys.argv[1] in {"validate", "current"}:
        try:
            handler = _validate_main if sys.argv[1] == "validate" else _current_main
            raise SystemExit(handler(sys.argv[1:]))
        except (OSError, json.JSONDecodeError, WorldClassScorecardError) as exc:
            print(f"v7_world_class_scorecard: {exc}", file=sys.stderr)
            raise SystemExit(2)
    economic_main()
