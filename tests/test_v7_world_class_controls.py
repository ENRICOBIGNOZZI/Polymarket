import importlib.util
import json
import os
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))
import v7_live_canary_orchestrator as canary  # noqa: E402
import v7_reconcile_account as reconcile  # noqa: E402
import v7_release_provenance as provenance  # noqa: E402
import v7_world_class_scorecard as world_class_scorecard  # noqa: E402


class WorldClassControlsTests(unittest.TestCase):
    def test_required_schemas_and_docs_exist_as_json_or_text(self) -> None:
        for path in ("config/v7_risk_tiers.json", "config/v7_attestation_trust.json", "schemas/v7/order_event.schema.json", "schemas/v7/fill_event.schema.json", "schemas/v7/private_state.schema.json", "schemas/v7/journal_entry.schema.json", "schemas/v7/reconciliation.schema.json", "schemas/v7/pnl_attestation.schema.json", "schemas/v7/public_pnl_attestation.schema.json", "schemas/v7/attestation_trust.schema.json", "schemas/v7/experiment.schema.json", "schemas/v7/experiment_run.schema.json", "schemas/v7/world_class_scorecard.schema.json", "schemas/v7/replay_parity.schema.json", "schemas/v7/simulator_calibration_support.schema.json", "schemas/v7/scenario_risk.schema.json"):
            value = json.loads((ROOT / path).read_text(encoding="utf-8"))
            self.assertIsInstance(value, dict)
        for path in ("docs/v7_world_class/economic_proof_protocol.md", "docs/v7_world_class/live_canary_runbook.md", "docs/security/v7_signer_threat_model.md", "docs/REPLAY_PARITY.md", "docs/EXPERIMENT_SCHEDULER.md", "docs/SIMULATOR_CALIBRATION.md", "docs/SCENARIO_RISK.md", "docs/LATENCY.md"):
            self.assertTrue((ROOT / path).is_file())

    def test_scorecard_never_infers_real_pnl_from_missing_evidence(self) -> None:
        value = json.loads((ROOT / "tests/fixtures/v7_world_class_scorecard_missing_evidence.json").read_text(encoding="utf-8"))
        self.assertEqual(value["state"], "MORE_EVIDENCE_REQUIRED")
        self.assertFalse(value["world_class_candidate"])
        self.assertIsNone(value["economics"]["realized_and_settled_net_pnl_base_units"])

    def test_published_status_is_validated_and_cannot_promote_automatically(self) -> None:
        value = json.loads((ROOT / "tests/fixtures/v7_world_class_status_missing_evidence.json").read_text(encoding="utf-8"))
        world_class_scorecard.validate_status(value)
        value["automatic_promotion"] = True
        with self.assertRaisesRegex(world_class_scorecard.WorldClassScorecardError, "automatic_promotion_forbidden"):
            world_class_scorecard.validate_status(value)

    def test_candidate_requires_all_evidence_hashes(self) -> None:
        value = json.loads((ROOT / "tests/fixtures/v7_world_class_status_missing_evidence.json").read_text(encoding="utf-8"))
        value.update({"state": "WORLD_CLASS_CANDIDATE", "world_class_candidate": True, "reason_codes": []})
        value["economics"] = {key: 1 for key in value["economics"]}
        value["execution"] = {key: 1 for key in value["execution"]}
        value["accounting"] = {"unresolved_reconciliation_breaks": 0, "independent_verifier_reproducible": True}
        value["reliability"] = {"chaos_test_pass_rate": 1, "production_recovery_verified": True}
        value["security"] = {"secret_scan_clean": True, "private_release_governance_verified": True}
        with self.assertRaisesRegex(world_class_scorecard.WorldClassScorecardError, "candidate_evidence_incomplete"):
            world_class_scorecard.validate_status(value)

    def test_current_status_is_generated_from_the_checkout_and_never_claims_real_pnl(self) -> None:
        value = world_class_scorecard.current_status(ROOT)
        world_class_scorecard.validate_status(value)
        self.assertEqual(value["state"], "MORE_EVIDENCE_REQUIRED")
        self.assertFalse(value["world_class_candidate"])
        self.assertIsNone(value["economics"]["realized_and_settled_net_pnl_base_units"])
        self.assertRegex(value["evidence"]["security_audit_sha256"], r"^[0-9a-f]{64}$")
        self.assertIn("working_tree_dirty", value["reason_codes"])

    def test_canary_is_blocked_and_cannot_execute(self) -> None:
        result = canary.plan(ROOT, stage="AUTHENTICATED_READ_ONLY")
        self.assertEqual(result["state"], "PRE_CANARY_BLOCKED")
        self.assertFalse(result["execution_performed"])
        self.assertNotIn("APPROVAL", json.dumps(result))

    def test_reconciliation_surfaces_breaks(self) -> None:
        report = {"model_sha": "a" * 40, "state": "MORE_EVIDENCE_REQUIRED", "reason_codes": ["missing"], "report_sha256": "b" * 64}
        result = reconcile.reconcile(report, run_id="run", exact_code_sha="a" * 40, period_start="2026-01-01T00:00:00Z", period_end="2026-01-02T00:00:00Z")
        self.assertEqual(result["state"], "MORE_EVIDENCE_REQUIRED")
        self.assertGreater(result["unresolved_break_count"], 0)

    def test_provenance_never_claims_signed_release(self) -> None:
        result = provenance.report(ROOT)
        self.assertFalse(result["signed_release_verified"])
        self.assertEqual(result["state"], "MORE_EVIDENCE_REQUIRED")


if __name__ == "__main__":
    unittest.main()
