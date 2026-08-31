#!/usr/bin/env python3
"""Fail-closed validation for V7 modes, live caps, claims, and run manifests.

This module is deliberately offline and contains no signing or order-submission code.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import re
from pathlib import Path
from typing import Any

SHA1_RE = re.compile(r"^[0-9a-f]{40}$")
SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
LIVE_MODES = {"MICRO_LIVE", "LIVE_RESTRICTED", "LIVE_SCALED"}
REQUIRED_MODES = {"RESEARCH_ZERO_AUTHORITY", "PAPER_SIMULATED", "SHADOW_LIVE_READ_ONLY", "MICRO_LIVE", "LIVE_RESTRICTED", "LIVE_SCALED", "DRAIN_ONLY", "CANCEL_ONLY", "KILLED"}
CAPABILITY_KEYS = {"public_ingress", "paper_orders", "authenticated_read", "signing", "submit_orders", "cancel_orders", "authoritative_pnl"}
RISK_TIERS = {"RESEARCH": "RESEARCH_ZERO_AUTHORITY", "PAPER": "PAPER_SIMULATED", "MICRO_LIVE": "MICRO_LIVE", "LIVE_RESTRICTED": "LIVE_RESTRICTED", "LIVE_SCALED": "LIVE_SCALED"}
RISK_LIMIT_KEYS = {"allowed_mode", "maximum_order_base_units", "maximum_gross_exposure_base_units", "maximum_event_loss_base_units", "maximum_daily_loss_base_units", "maximum_open_order_count"}


class ControlPlaneError(ValueError):
    pass


def load_json(path: Path) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ControlPlaneError(f"invalid_json:{path}") from exc
    if not isinstance(value, dict):
        raise ControlPlaneError(f"object_required:{path}")
    return value


def sha256_file(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def validate_execution_modes(value: dict[str, Any]) -> None:
    required_root = {"schema", "version", "default_execution_mode", "checked_in_live_caps", "modes"}
    if set(value) != required_root or value.get("schema") != "polymarket_v7_execution_modes_v1" or value.get("version") != 7 or value.get("default_execution_mode") not in REQUIRED_MODES:
        raise ControlPlaneError("execution_modes:schema")
    checked_in_caps = value["checked_in_live_caps"]
    expected_caps = {"maximum_order_base_units", "maximum_gross_exposure_base_units", "maximum_event_loss_base_units", "maximum_daily_loss_base_units", "maximum_open_order_count"}
    if not isinstance(checked_in_caps, dict) or set(checked_in_caps) != expected_caps or any(isinstance(limit, bool) or not isinstance(limit, int) or limit != 0 for limit in checked_in_caps.values()):
        raise ControlPlaneError("execution_modes:checked_in_live_caps")
    modes = value.get("modes")
    if not isinstance(modes, dict) or set(modes) != REQUIRED_MODES:
        raise ControlPlaneError("execution_modes:missing_or_unknown_mode")
    for name, caps in modes.items():
        if not isinstance(caps, dict) or set(caps) != CAPABILITY_KEYS or not all(isinstance(v, bool) for v in caps.values()):
            raise ControlPlaneError(f"execution_modes:capabilities:{name}")
        if caps["submit_orders"] and not caps["signing"]:
            raise ControlPlaneError(f"execution_modes:unsafe_submit:{name}")
        if name not in LIVE_MODES and (caps["signing"] or caps["submit_orders"] or caps["authoritative_pnl"]):
            raise ControlPlaneError(f"execution_modes:nonlive_value_authority:{name}")
        if name == "PAPER_SIMULATED" and not caps["paper_orders"]:
            raise ControlPlaneError("execution_modes:paper_not_simulated")


def validate_live_caps(value: dict[str, Any]) -> None:
    expected = {"schema_version", "live_enabled", "maximum_order_base_units", "maximum_gross_exposure_base_units", "maximum_event_loss_base_units", "maximum_daily_loss_base_units", "maximum_open_order_count", "note"}
    if set(value) != expected or value.get("schema_version") != 1 or value.get("live_enabled") is not False:
        raise ControlPlaneError("live_caps:not_checked_in_zero_disabled")
    for key in expected - {"schema_version", "live_enabled", "note"}:
        if isinstance(value[key], bool) or not isinstance(value[key], int) or value[key] != 0:
            raise ControlPlaneError(f"live_caps:nonzero:{key}")


def validate_risk_tiers(value: dict[str, Any]) -> None:
    expected = {"schema_version", "checked_in_execution_mode", "automatic_promotion", "automatic_live_cap_increase", "tiers", "note"}
    if (set(value) != expected or value.get("schema_version") != 1 or value.get("checked_in_execution_mode") != "PAPER_SIMULATED"
            or value.get("automatic_promotion") is not False or value.get("automatic_live_cap_increase") is not False):
        raise ControlPlaneError("risk_tiers:schema")
    tiers = value.get("tiers")
    if not isinstance(tiers, dict) or set(tiers) != set(RISK_TIERS):
        raise ControlPlaneError("risk_tiers:tiers")
    for name, allowed_mode in RISK_TIERS.items():
        limits = tiers[name]
        if not isinstance(limits, dict) or set(limits) != RISK_LIMIT_KEYS or limits.get("allowed_mode") != allowed_mode:
            raise ControlPlaneError(f"risk_tiers:shape:{name}")
        for key, limit in limits.items():
            if key != "allowed_mode" and (isinstance(limit, bool) or not isinstance(limit, int) or limit != 0):
                raise ControlPlaneError(f"risk_tiers:nonzero:{name}:{key}")


def validate_claim_registry(value: dict[str, Any]) -> None:
    claims = value.get("claims")
    required = {"TECHNICALLY_VALIDATED", "DEPLOYED_READ_ONLY", "DEPLOYED_PAPER", "DEPLOYED_MICRO_LIVE", "RECONCILED", "PROFITABILITY_NOT_TESTABLE", "MORE_EVIDENCE_REQUIRED", "PROFITABILITY_REJECTED", "REAL_PNL_VERIFIED", "WORLD_CLASS_CANDIDATE"}
    if value.get("schema_version") != 1 or value.get("claim_policy") != "exact_sha_evidence_only" or not isinstance(claims, dict) or set(claims) != required:
        raise ControlPlaneError("claims:schema")
    for name, rule in claims.items():
        if not isinstance(rule, dict) or set(rule) != {"requires"} or not isinstance(rule["requires"], list) or not rule["requires"] or not all(isinstance(item, str) and item for item in rule["requires"]):
            raise ControlPlaneError(f"claims:requirements:{name}")
    if "REAL_PNL_VERIFIED" not in claims["WORLD_CLASS_CANDIDATE"]["requires"]:
        raise ControlPlaneError("claims:world_class_must_require_real_pnl")


def validate_control_manifest(value: dict[str, Any], modes: dict[str, Any]) -> None:
    required = {"schema_version", "exact_code_sha", "build_manifest_hash", "config_bundle_hash", "strategy_registry_hash", "model_registry_hash", "policy_hash", "dataset_manifest_hash", "execution_mode", "wallet_id_hash", "signer_session_id_hash", "server_id", "region", "run_id", "start_time"}
    if set(value) != required or value.get("schema_version") != 1:
        raise ControlPlaneError("manifest:schema")
    if not isinstance(value["exact_code_sha"], str) or not SHA1_RE.fullmatch(value["exact_code_sha"]):
        raise ControlPlaneError("manifest:exact_code_sha")
    for key in {"build_manifest_hash", "config_bundle_hash", "strategy_registry_hash", "model_registry_hash", "policy_hash", "dataset_manifest_hash", "wallet_id_hash", "signer_session_id_hash"}:
        if not isinstance(value[key], str) or not SHA256_RE.fullmatch(value[key]):
            raise ControlPlaneError(f"manifest:{key}")
    mode = value["execution_mode"]
    if mode not in modes["modes"]:
        raise ControlPlaneError("manifest:execution_mode")
    if not all(isinstance(value[key], str) and value[key] for key in {"server_id", "region", "run_id", "start_time"}):
        raise ControlPlaneError("manifest:identity")


def validate_repository(root: Path, manifest: Path | None = None) -> dict[str, str]:
    modes_path = root / "config/v7_execution_modes.json"
    caps_path = root / "config/v7_live_caps_zero.json"
    risk_tiers_path = root / "config/v7_risk_tiers.json"
    claims_path = root / "config/v7_claim_registry.json"
    modes, caps, risk_tiers, claims = load_json(modes_path), load_json(caps_path), load_json(risk_tiers_path), load_json(claims_path)
    validate_execution_modes(modes)
    validate_live_caps(caps)
    validate_risk_tiers(risk_tiers)
    validate_claim_registry(claims)
    paper = load_json(root / "config/paper_v7.json")
    v7 = paper.get("v7")
    if not isinstance(v7, dict) or any(v7.get(key) is not expected for key, expected in {"paper_only": True, "authenticated_execution": False, "real_order_submission": False}.items()):
        raise ControlPlaneError("paper_config:not_paper_only")
    if manifest is not None:
        validate_control_manifest(load_json(manifest), modes)
    return {str(path.relative_to(root)): sha256_file(path) for path in (modes_path, caps_path, risk_tiers_path, claims_path)}


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--root", type=Path, default=Path(__file__).resolve().parents[1])
    parser.add_argument("--manifest", type=Path)
    args = parser.parse_args()
    print(json.dumps({"schema_version": 1, "control_plane": "VALID", "hashes": validate_repository(args.root.resolve(), args.manifest)}, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
