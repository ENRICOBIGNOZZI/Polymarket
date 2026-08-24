from __future__ import annotations

import importlib.util
import json
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "scripts" / "integration_gate.py"

spec = importlib.util.spec_from_file_location("integration_gate", SCRIPT)
assert spec and spec.loader
integration_gate = importlib.util.module_from_spec(spec)
spec.loader.exec_module(integration_gate)


class IntegrationResearchProvenanceTest(unittest.TestCase):
    def candidate(self) -> dict:
        checks = [
            {
                "__typename": "CheckRun",
                "name": name,
                "status": "COMPLETED",
                "conclusion": "SUCCESS",
            }
            for name in (
                "build-test (Release)",
                "build-test (Debug)",
                "live-paper-smoke",
                "validate",
                "enforce",
            )
        ]
        return {
            "number": 200,
            "headRefName": "integration/example",
            "isDraft": False,
            "labels": [
                {"name": "approved-for-integration"},
                {"name": "single-model-reviewed"},
                {"name": "administrator-approved"},
            ],
            "mergeStateStatus": "CLEAN",
            "statusCheckRollup": checks,
            "body": (
                "- [x] Approved research integration into the single champion\n"
                "Source research PR/branch/commit: #123\n"
            ),
        }

    def source(self) -> dict:
        return {
            "number": 123,
            "headRefName": "research/example",
            "isDraft": True,
            "state": "OPEN",
            "labels": [{"name": "research-approved"}],
        }

    def test_approved_numbered_source_passes(self):
        self.assertEqual(
            integration_gate.validate_candidate(self.candidate(), self.source()),
            [],
        )

    def test_source_without_research_approval_is_rejected(self):
        source = self.source()
        source["labels"] = []
        errors = integration_gate.validate_candidate(self.candidate(), source)
        self.assertIn("source research PR is not research-approved", errors)

    def test_branch_only_source_cannot_be_selected(self):
        candidate = self.candidate()
        candidate["body"] = candidate["body"].replace("#123", "experiment/example")
        self.assertEqual(integration_gate.select_candidates([candidate]), [])
        errors = integration_gate.validate_candidate(candidate, self.source())
        self.assertTrue(any("numbered PR" in error for error in errors))

    def test_mismatched_source_number_is_rejected(self):
        source = self.source()
        source["number"] = 124
        errors = integration_gate.validate_candidate(self.candidate(), source)
        self.assertTrue(any("expected #123" in error for error in errors))

    def test_source_integration_labels_are_rejected(self):
        source = self.source()
        source["labels"].append({"name": "administrator-approved"})
        errors = integration_gate.validate_candidate(self.candidate(), source)
        self.assertTrue(any("carries integration labels" in error for error in errors))

    def test_current_paper_gate_is_restored_to_validated_baseline(self):
        config = json.loads((ROOT / "config" / "paper_v4.json").read_text(encoding="utf-8"))
        self.assertEqual(config["min_net_edge"], 0.005)


if __name__ == "__main__":
    unittest.main()
