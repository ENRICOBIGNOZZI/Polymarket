from __future__ import annotations

import importlib.util
import sys
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "scripts" / "lf_v7_execution_cost_stress_audit.py"
spec = importlib.util.spec_from_file_location("lf_v7_execution_cost_stress_audit_test", SCRIPT)
assert spec and spec.loader
audit = importlib.util.module_from_spec(spec)
sys.modules[spec.name] = audit
spec.loader.exec_module(audit)


class V7ExecutionCostStressAuditTest(unittest.TestCase):
    def test_disjoint_cost_vector_can_reverse_two_x_pnl_sign(self) -> None:
        result = audit.deterministic_counterexample()
        self.assertAlmostEqual(result["incumbent_recognized_baseline_cost"], 0.006)
        self.assertAlmostEqual(result["full_disjoint_baseline_cost"], 0.024)
        self.assertAlmostEqual(result["stress"]["1.5"]["incumbent"], 0.017)
        self.assertAlmostEqual(result["stress"]["1.5"]["full_vector"], 0.008)
        self.assertAlmostEqual(result["stress"]["2.0"]["incumbent"], 0.014)
        self.assertAlmostEqual(result["stress"]["2.0"]["full_vector"], -0.004)
        self.assertTrue(result["two_x_sign_disagreement"])

    def test_current_sensitive_sidecar_does_not_verify_full_cost_vector(self) -> None:
        source = audit.inspect_source(ROOT)
        self.assertTrue(source["uses_first_number"])
        self.assertTrue(source["mentions_fee"])
        self.assertTrue(source["mentions_slippage_cost"])
        self.assertFalse(source["mentions_unwind_cost"])
        self.assertFalse(source["mentions_capital_cost"])
        self.assertFalse(source["mentions_latency_cost"])

    def test_existing_execution_evidence_regression_is_now_in_ctest(self) -> None:
        registration = audit.inspect_test_registration(ROOT)
        self.assertTrue(registration["registered_in_cmake"])

    def test_audit_remains_research_only_and_fail_closed(self) -> None:
        report = audit.build_report(ROOT)
        self.assertTrue(report["research_only"])
        self.assertEqual(report["decision"], "MORE_EVIDENCE_REQUIRED")
        self.assertIn("cost_stress_does_not_verify_complete_disjoint_cost_vector", report["defects"])
        self.assertTrue(report["material"])


if __name__ == "__main__":
    unittest.main()
