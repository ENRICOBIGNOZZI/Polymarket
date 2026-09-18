#!/usr/bin/env python3
"""Fail-closed readiness gate for six-asset multi-crypto V7."""
from __future__ import annotations

import argparse
import json
import os
import shutil
import time
from pathlib import Path
from typing import Any

from v7_lead_lag_replay import ReplayError, digest
from v7_multi_crypto_execution_accounting_contract import validate as validate_execution_contract
from v7_multi_crypto_forward_freeze import DRAFT_SCHEMA, FROZEN_SCHEMA, validate_draft

SCHEMA = "polymarket_v7_multi_crypto_readiness_gate_v1"


def load(path: Path | None) -> dict[str, Any]:
    if path is None or not path.exists():
        return {}
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    return value if isinstance(value, dict) else {}


def atomic_json(path: Path, value: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + f".tmp.{os.getpid()}")
    tmp.write_text(json.dumps(value, indent=2, sort_keys=True, allow_nan=False) + "\n", encoding="utf-8")
    os.replace(tmp, path)


def safe_report(value: dict[str, Any]) -> bool:
    return value.get("paper_only") is True and value.get("execution_authority") is False \
        and value.get("real_order_submission") is False


def valid_frozen_protocol(value: dict[str, Any]) -> bool:
    frozen = value.get("frozen_protocol")
    protocol_hash = str(value.get("protocol_hash") or "")
    if (value.get("schema") != FROZEN_SCHEMA
            or value.get("paper_only") is not True
            or value.get("authenticated_execution") is not False
            or value.get("real_order_submission") is not False
            or value.get("real_capital_at_risk") is not False
            or value.get("automatic_promotion") is not False
            or value.get("entry_authority") is not False
            or not isinstance(frozen, dict)
            or frozen.get("schema") != FROZEN_SCHEMA
            or len(protocol_hash) != 64
            or digest(frozen) != protocol_hash):
        return False
    draft = dict(frozen); draft["schema"] = DRAFT_SCHEMA
    try:
        validate_draft(draft)
    except (ReplayError, TypeError, ValueError, KeyError):
        return False
    return True


def evaluate(root: Path, *, shock_report: Path | None, research_report: Path | None,
             latency_report: Path | None, forward_protocol: Path | None = None) -> dict[str, Any]:
    execution = validate_execution_contract(root)
    ledger = load(root / "docs/v7_multi_crypto/ledger_recovery_audit_20260917.json")
    shadow = load(root / "docs/v7_multi_crypto/shadow_runtime_smoke_20260917.json")
    shock = load(shock_report); research = load(research_report); latency = load(latency_report)
    forward = load(forward_protocol)
    london = load(root / "config/v7_london_az_shootout.json")
    shock_policy = load(root / "config/v7_multi_crypto_shock_calibration.json")
    shadow_policy = load(root / "config/v7_multi_crypto_shadow_runtime.json")
    disk_free_bytes = shutil.disk_usage(root).free
    disk_required_bytes = int(shadow_policy.get("minimum_free_gib") or 0) * 1024**3
    blockers: list[str] = []
    if disk_required_bytes <= 0 or disk_free_bytes < disk_required_bytes:
        blockers.append("DISK_FREE_BELOW_RUNTIME_POLICY")

    if execution.get("state") != "VERIFIED_PARTITIONED_PAPER_CONTRACT" or execution.get("new_risk_authorized") is not False:
        blockers.append("EXECUTION_ACCOUNTING_CONTRACT_NOT_VERIFIED")
    if (ledger.get("ledger_rows") or 0) <= 0 or ledger.get("open_orders") != 0 \
            or ledger.get("fill_final_one_to_one") is not True or ledger.get("spool_files") != 0:
        blockers.append("LEDGER_RECOVERY_AUDIT_NOT_CLEAN")
    if shadow.get("economic_evidence") is not False or shadow.get("execution_authority") is not False:
        blockers.append("SHADOW_RUNTIME_EVIDENCE_MISSING_OR_INVALID")

    if not safe_report(shock) or shock.get("status") != "TRAINING_DISTRIBUTION_CALIBRATED":
        blockers.append("SHOCK_CALIBRATION_INSUFFICIENT")
    if shock_policy.get("selected_threshold") is None:
        blockers.append("SHOCK_TRIGGER_THRESHOLD_NOT_FROZEN")
    if not safe_report(research) or research.get("status") != "RESEARCH_OOS_AVAILABLE" \
            or int(research.get("time_clusters") or 0) < 20:
        blockers.append("INDEPENDENT_OOS_REPRICING_EVIDENCE_INSUFFICIENT")
    if research.get("economic_evidence") is not True:
        blockers.append("NO_EXECUTABLE_ECONOMIC_PNL_EVIDENCE")

    runtime = shadow.get("runtime") if isinstance(shadow.get("runtime"), dict) else {}
    if (shadow.get("paper_only") is not True or shadow.get("execution_authority") is not False
            or runtime.get("last_runtime_state") != "RUNNING_SHADOW"
            or int(runtime.get("external_ready_assets") or 0) != 6
            or int(runtime.get("oracle_healthy_assets") or 0) != 6
            or runtime.get("book_evidence_complete") is not True
            or runtime.get("label_evidence_complete") is not True):
        blockers.append("UNIFIED_SHADOW_RUNTIME_NOT_VERIFIED")

    if (latency.get("paper_only") is not True or latency.get("execution_authority") is not False
            or int(latency.get("common_observable_candidate_count") or 0) <= 0):
        blockers.append("LATENCY_CAPACITY_MECHANICS_MISSING")
    elif latency.get("economic_evidence") is not True:
        blockers.append("LATENCY_CAPACITY_NOT_COSTED_ECONOMIC_EVIDENCE")

    selected_zone = london.get("selected_zone_id") or london.get("selected_zone")
    if not selected_zone:
        blockers.append("LONDON_AZ_NOT_SELECTED")
    if not valid_frozen_protocol(forward):
        blockers.append("MULTI_CRYPTO_FORWARD_PROTOCOL_NOT_FROZEN")

    blockers = sorted(set(blockers))
    return {
        "schema": SCHEMA, "version": 1, "timestamp_ns": time.time_ns(),
        "state": "READY_FOR_PAPER_FORWARD" if not blockers else "BLOCKED_EVIDENCE",
        "paper_only": True, "authenticated_execution": False, "real_order_submission": False,
        "real_capital_at_risk": False, "execution_authority": False,
        "promotion_authorized": False, "paper_forward_authorized": False,
        "blockers": blockers,
        "execution_contract_state": execution.get("state"),
        "shock_status": shock.get("status"), "shock_time_clusters": int(shock.get("time_clusters") or 0),
        "repricing_status": research.get("status"), "repricing_time_clusters": int(research.get("time_clusters") or 0),
        "latency_candidate_count": int(latency.get("common_observable_candidate_count") or 0),
        "forward_protocol_valid": valid_frozen_protocol(forward),
        "forward_protocol_hash": str(forward.get("protocol_hash") or "") or None,
        "disk_free_bytes": disk_free_bytes, "disk_required_bytes": disk_required_bytes,
        "claim_boundary": "Readiness gate only. It cannot authorize live or PAPER new-risk execution.",
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--repository-root", type=Path, default=Path.cwd())
    parser.add_argument("--shock-report", type=Path)
    parser.add_argument("--research-report", type=Path)
    parser.add_argument("--latency-report", type=Path)
    parser.add_argument("--forward-protocol", type=Path)
    parser.add_argument("--output", type=Path)
    args = parser.parse_args(); root = args.repository_root.resolve()
    value = evaluate(root, shock_report=args.shock_report, research_report=args.research_report,
                     latency_report=args.latency_report, forward_protocol=args.forward_protocol)
    if args.output:
        atomic_json(args.output, value)
    print(json.dumps(value, indent=2, sort_keys=True))
    return 0 if value["state"] == "READY_FOR_PAPER_FORWARD" else 2


if __name__ == "__main__":
    raise SystemExit(main())
