#!/usr/bin/env python3
"""Audit the local V7 implementation inventory without making evidence claims."""
from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path
from typing import Any


REQUIRED_FILES = (
    "docs/v7_world_class/target_architecture.md", "docs/v7_world_class/claim_policy.md",
    "docs/v7_world_class/economic_proof_protocol.md", "docs/v7_world_class/live_canary_runbook.md",
    "docs/security/v7_signer_threat_model.md", "docs/runbooks/v7_startup.md", "docs/runbooks/v7_restore.md",
    "docs/REPLAY_PARITY.md", "docs/EXPERIMENT_SCHEDULER.md", "docs/SIMULATOR_CALIBRATION.md", "docs/SCENARIO_RISK.md",
    "docs/PLATFORM_CONTRACT.md",
    "config/v7_execution_modes.json", "config/v7_platform_contract.json", "config/v7_live_caps_zero.json",
    "config/v7_authority_registry.json",
    "config/v7_risk_tiers.json", "config/v7_claim_registry.json", "config/v7_runtime_supervision.json",
    "config/v7_attestation_trust.json", "config/v7_polymarket_v2_contracts.json",
    "schemas/v7/order_event.schema.json", "schemas/v7/fill_event.schema.json", "schemas/v7/private_state.schema.json",
    "schemas/v7/journal_entry.schema.json", "schemas/v7/reconciliation.schema.json", "schemas/v7/pnl_attestation.schema.json",
    "schemas/v7/experiment.schema.json", "schemas/v7/experiment_run.schema.json", "schemas/v7/world_class_scorecard.schema.json",
    "schemas/v7/platform_contract.schema.json", "schemas/v7/platform_contract_archive.schema.json", "schemas/v7/session_registry.schema.json",
    "schemas/v7/replay_parity.schema.json", "schemas/v7/simulator_calibration_support.schema.json", "schemas/v7/scenario_risk.schema.json",
    "schemas/v7/attestation_trust.schema.json", "schemas/v7/public_pnl_attestation.schema.json",
    "schemas/v7/control_manifest.schema.json",
    "schemas/v7/opportunity_envelope.schema.json",
    "scripts/v7_platform_drift_monitor.py", "scripts/v7_platform_contract_archive.py", "scripts/v7_reconcile_account.py",
    "scripts/v7_real_pnl_verifier.py", "scripts/v7_generate_pnl_attestation.py", "scripts/v7_verify_pnl_attestation.py",
    "scripts/v7_live_canary_orchestrator.py", "scripts/v7_world_class_scorecard.py", "scripts/v7_security_audit.py",
    "scripts/v7_release_provenance.py", "scripts/v7_dataset_manifest.py", "scripts/v7_replay_parity.py",
    "scripts/v7_experiment_registry.py", "scripts/v7_experiment_scheduler.py", "scripts/v7_maker_probe_design.py",
    "scripts/v7_simulator_calibration_support.py", "scripts/v7_scenario_risk.py", "scripts/v7_regional_shootout.py",
    "scripts/v7_implementation_audit.py", "scripts/v7_entropy_secret_scan.py", "scripts/v7_protocol_fuzz.py",
    "scripts/v7_session_registry.py", "scripts/v7_artifact_store.py", "scripts/v7_execution_provenance.py",
    "scripts/v7_control_plane.py", "scripts/v7_live_capability.py", "scripts/v7_signer_gateway.py",
    "scripts/v7_authority_contract.py", "scripts/v7_opportunity.py",
    "scripts/v7_build_manifest.py", "scripts/verify_v7.sh",
    "monitoring/exporter_v7.py", "monitoring/v7_alerts.yml", "monitoring/v7_monitoring_manifest.json",
    "tests/test_v7_real_pnl_verifier.py", "tests/test_v7_replay_parity.py", "tests/test_v7_experiment_scheduler.py",
    "tests/test_v7_simulator_calibration_support.py", "tests/test_v7_scenario_risk.py", "tests/test_v7_regional_shootout.py",
    "tests/test_v7_implementation_audit.py", "tests/test_v7_chaos_runtime.py", "tests/test_v7_world_class_controls.py",
    "tests/test_v7_verify_contract.py", "tests/test_v7_platform_contract.py",
    "tests/test_v7_platform_contract_archive.py", "tests/test_v7_entropy_secret_scan.py",
    "tests/test_v7_protocol_fuzz.py", "tests/test_v7_session_registry.py",
    "tests/test_v7_authority_contract.py", "tests/test_v7_opportunity.py",
)


class ImplementationAuditError(ValueError):
    pass


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def audit(root: Path) -> dict[str, Any]:
    root = Path(root).resolve()
    if not root.is_dir() or root.is_symlink():
        raise ImplementationAuditError("root:invalid")
    missing = [relative for relative in REQUIRED_FILES if not (root / relative).is_file()]
    hashes = {relative: _sha256(root / relative) for relative in REQUIRED_FILES if relative not in missing}
    return {
        "schema": "polymarket_v7_implementation_inventory_v1", "required_file_count": len(REQUIRED_FILES),
        "present_file_count": len(hashes), "missing_files": missing, "source_sha256": hashes,
        "implementation_complete": not missing,
        "limitations": [
            "This inventory proves only local code, schemas, tests, monitoring and runbooks exist.",
            "It does not prove CI execution, deployment, private controls, live operation, profitability or real PnL.",
        ],
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, default=Path("."))
    args = parser.parse_args(argv)
    try:
        result = audit(args.root)
        print(json.dumps(result, sort_keys=True))
        return 0 if result["implementation_complete"] else 1
    except ImplementationAuditError as exc:
        print(f"v7_implementation_audit: {exc}", file=__import__("sys").stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
