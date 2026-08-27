from __future__ import annotations

import importlib.util
import sys
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "scripts" / "lf_v7_multileg_completion_evidence_audit.py"
spec = importlib.util.spec_from_file_location("lf_v7_multileg_completion_evidence_audit_test", SCRIPT)
assert spec and spec.loader
audit_module = importlib.util.module_from_spec(spec)
sys.modules[spec.name] = audit_module
spec.loader.exec_module(audit_module)


class V7MultilegCompletionEvidenceAuditTest(unittest.TestCase):
    def test_one_leg_per_bundle_can_pass_fill_gates_with_zero_joint_completion(self) -> None:
        report = audit_module.audit()
        row = report["counterexamples"]["twenty_two_leg_bundles_one_leg_filled_each"]
        self.assertEqual(row["unique_bundle_submissions"], 20)
        self.assertEqual(row["unique_leg_entry_fills"], 20)
        self.assertEqual(row["incumbent_fill_rate"], 1.0)
        self.assertTrue(row["default_min_fills_satisfied"])
        self.assertTrue(row["one_percent_fill_gate_satisfied"])
        self.assertEqual(row["economic_joint_completions"], 0)
        self.assertEqual(row["economic_joint_completion_rate"], 0.0)
        self.assertTrue(row["joint_completion_gate_should_fail"])

    def test_leg_fill_over_bundle_submission_ratio_is_not_probability_bounded(self) -> None:
        report = audit_module.audit()
        row = report["counterexamples"]["four_five_leg_bundles_all_legs_filled"]
        self.assertEqual(row["unique_bundle_submissions"], 4)
        self.assertEqual(row["unique_leg_entry_fills"], 20)
        self.assertEqual(row["incumbent_fill_rate"], 5.0)
        self.assertEqual(row["economic_joint_completions"], 4)
        self.assertEqual(row["economic_joint_completion_rate"], 1.0)
        self.assertGreater(row["incumbent_fill_rate"], 1.0)

    def test_audit_is_source_bound_and_paper_only(self) -> None:
        report = audit_module.audit()
        self.assertEqual(report["base_main_sha"], audit_module.BASE_SHA)
        self.assertEqual(report["source"], "scripts/v7_execution_evidence.py")
        self.assertIn("RUN/bundle_ledger.csv", report["source_contract"]["relative_value_execution_paths"])
        self.assertIn("RUN/intents.csv", report["source_contract"]["relative_value_submission_paths"])
        self.assertEqual(report["decision"], "MORE_EVIDENCE_REQUIRED")
        self.assertTrue(report["paper_only"])
        self.assertFalse(report["authenticated_execution"])
        self.assertFalse(report["production_mutated"])


if __name__ == "__main__":
    unittest.main()
