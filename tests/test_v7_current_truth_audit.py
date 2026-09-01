from __future__ import annotations

import importlib.util
import sys
import unittest
from datetime import datetime, timezone
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


def load():
    spec = importlib.util.spec_from_file_location("v7_current_truth_audit", ROOT / "scripts/v7_current_truth_audit.py")
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


audit = load()


class CurrentTruthAuditTests(unittest.TestCase):
    def test_report_is_redacted_and_hashes_current_sources(self) -> None:
        report = audit.audit(ROOT, now=datetime(2026, 8, 31, tzinfo=timezone.utc),
                             secret_report={
                                 "history_scanned": True,
                                 "finding_count": 1,
                                 "findings": [{
                                     "kind": "assigned_secret",
                                     "location": "history:abc:note.txt:1",
                                 "fingerprint": "a" * 16,
                                 }],
                             }, entropy_report={
                                 "history_scanned": True,
                                 "finding_count": 0,
                                 "findings": [],
                             })
        self.assertEqual(report["audit_timestamp"], "2026-08-31T00:00:00Z")
        self.assertEqual(set(report["source_sha256"]), set(audit.SOURCE_PATHS))
        self.assertIn("scripts/v7_real_pnl_scorecard.py", report["source_sha256"])
        self.assertIn("scripts/v7_reconcile_account.py", report["source_sha256"])
        self.assertIn("scripts/v7_dataset_manifest.py", report["source_sha256"])
        self.assertIn("scripts/v7_maker_probe_design.py", report["source_sha256"])
        self.assertIn("scripts/v7_simulator_calibration_support.py", report["source_sha256"])
        self.assertIn("schemas/v7/simulator_calibration_support.schema.json", report["source_sha256"])
        self.assertIn("scripts/v7_scenario_risk.py", report["source_sha256"])
        self.assertIn("schemas/v7/scenario_risk.schema.json", report["source_sha256"])
        self.assertIn("scripts/v7_regional_shootout.py", report["source_sha256"])
        self.assertIn("scripts/v7_implementation_audit.py", report["source_sha256"])
        self.assertTrue(report["claims"]["IMPLEMENTATION_COMPLETE"])
        self.assertTrue(report["implementation_inventory"]["implementation_complete"])
        self.assertIn("scripts/v7_experiment_registry.py", report["source_sha256"])
        self.assertIn("scripts/v7_experiment_scheduler.py", report["source_sha256"])
        self.assertIn("schemas/v7/experiment_run.schema.json", report["source_sha256"])
        self.assertIn("scripts/v7_protocol_fuzz.py", report["source_sha256"])
        self.assertIn("scripts/verify_v7.sh", report["source_sha256"])
        self.assertIn("tests/test_v7_verify_contract.py", report["source_sha256"])
        self.assertIn("scripts/v7_replay_parity.py", report["source_sha256"])
        self.assertIn("schemas/v7/replay_parity.schema.json", report["source_sha256"])
        self.assertIn("schemas/v7/experiment.schema.json", report["source_sha256"])
        self.assertIn("scripts/v7_real_pnl_evidence.py", report["source_sha256"])
        self.assertIn("scripts/v7_execution_provenance.py", report["source_sha256"])
        self.assertIn("scripts/v7_artifact_store.py", report["source_sha256"])
        self.assertIn("scripts/v7_platform_contract_archive.py", report["source_sha256"])
        self.assertIn("schemas/v7/platform_contract_archive.schema.json", report["source_sha256"])
        self.assertIn("scripts/v7_session_registry.py", report["source_sha256"])
        self.assertIn("schemas/v7/session_registry.schema.json", report["source_sha256"])
        self.assertIn("scripts/v7_verify_pnl_attestation.py", report["source_sha256"])
        self.assertIn("config/v7_attestation_trust.json", report["source_sha256"])
        self.assertIn("schemas/v7/public_pnl_attestation.schema.json", report["source_sha256"])
        self.assertFalse(report["security"]["safe_for_authenticated_execution"])
        self.assertEqual(report["security"]["history_pattern_secret_scan_findings"], 1)
        self.assertEqual(report["security"]["history_entropy_secret_scan_findings"], 0)
        self.assertTrue(report["control_integrity"]["checked_in_attestation_trust_empty"])
        self.assertTrue(report["control_integrity"]["deprecated_control_artifacts_absent"])
        self.assertNotIn("findings", report["security"])
        self.assertEqual(report["security"]["audit_state"], "SECURITY_BLOCKED")
        self.assertIn("MORE_EVIDENCE_REQUIRED = TRUE", audit.markdown(report))


if __name__ == "__main__":
    unittest.main()
