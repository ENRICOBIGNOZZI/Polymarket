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


class WorldClassControlsTests(unittest.TestCase):
    def test_required_schemas_and_docs_exist_as_json_or_text(self) -> None:
        for path in ("config/v7_risk_tiers.json", "schemas/v7/order_event.schema.json", "schemas/v7/fill_event.schema.json", "schemas/v7/private_state.schema.json", "schemas/v7/journal_entry.schema.json", "schemas/v7/reconciliation.schema.json", "schemas/v7/pnl_attestation.schema.json", "schemas/v7/experiment.schema.json", "schemas/v7/world_class_scorecard.schema.json"):
            value = json.loads((ROOT / path).read_text(encoding="utf-8"))
            self.assertIsInstance(value, dict)
        for path in ("docs/v7_world_class/economic_proof_protocol.md", "docs/v7_world_class/live_canary_runbook.md", "docs/security/v7_signer_threat_model.md"):
            self.assertTrue((ROOT / path).is_file())

    def test_scorecard_never_infers_real_pnl_from_missing_evidence(self) -> None:
        value = json.loads((ROOT / "artifacts/v7_world_class/scorecard.json").read_text(encoding="utf-8"))
        self.assertEqual(value["state"], "MORE_EVIDENCE_REQUIRED")
        self.assertFalse(value["world_class_candidate"])
        self.assertIsNone(value["economics"]["realized_and_settled_net_pnl_base_units"])

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
