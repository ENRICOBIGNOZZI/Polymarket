from __future__ import annotations

import importlib.util
import math
import sys
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "scripts/lf_v7_local_factor_current_state_audit.py"
SPEC = importlib.util.spec_from_file_location("lf_v7_local_factor_current_state_audit", SCRIPT)
assert SPEC is not None and SPEC.loader is not None
AUDIT = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = AUDIT
SPEC.loader.exec_module(AUDIT)


class LocalFactorCurrentStateAuditTests(unittest.TestCase):
    def test_source_contract_uses_historical_endpoint_for_gate_and_forecast(self) -> None:
        contract = AUDIT.source_contract(ROOT)
        self.assertTrue(contract["historical_endpoint_used_for_post_multiplicity_z_gate"])
        self.assertTrue(contract["historical_endpoint_used_for_n_step_signal_forecast"])
        self.assertTrue(contract["driver_passes_only_current_target_mids_to_pair_signal"])
        self.assertTrue(contract["orientation_pc1_returns_temporal_scores_without_current_projection_state"])
        self.assertFalse(contract["current_book_pair_residual_reconstruction_available"])

    def test_reverted_current_state_can_still_pass_historical_z_gate(self) -> None:
        case = AUDIT.counterexamples()["reverted_between_completed_bucket_and_current_book"]
        self.assertTrue(case["incumbent_historical_gate_passes"])
        self.assertFalse(case["current_state_gate_passes"])
        self.assertAlmostEqual(case["forecast_magnitude_overstatement"], 20.0, places=10)
        self.assertAlmostEqual(case["incumbent_forecast_residual_change"][0], -1.475712, places=12)
        self.assertAlmostEqual(case["current_state_forecast_residual_change"][0], -0.0737856, places=12)

    def test_new_current_dislocation_can_be_missed_by_historical_gate(self) -> None:
        case = AUDIT.counterexamples()["new_dislocation_after_completed_bucket"]
        self.assertFalse(case["incumbent_historical_gate_passes"])
        self.assertTrue(case["current_state_gate_passes"])
        self.assertGreater(abs(case["current_state_forecast_residual_change"][0]), 1.0)

    def test_current_state_sign_flip_reverses_trade_direction(self) -> None:
        case = AUDIT.counterexamples()["sign_flip_after_completed_bucket"]
        self.assertEqual(case["incumbent_sides"], ["NO", "YES"])
        self.assertEqual(case["current_state_sides"], ["YES", "NO"])
        self.assertEqual(case["direction_reversed"], [True, True])

    def test_report_is_fail_closed_research_only(self) -> None:
        report = AUDIT.build_report(ROOT)
        self.assertEqual(report["finding"], "CURRENT_BOOK_RESIDUAL_STATE_NOT_RECONSTRUCTED")
        self.assertEqual(report["decision"], "MORE_EVIDENCE_REQUIRED")
        self.assertTrue(report["material_structural_blocker"])
        self.assertTrue(report["safety"]["paper_only"])
        self.assertFalse(report["safety"]["authenticated_execution"])
        self.assertFalse(report["safety"]["main_or_paper_validated_mutation"])


if __name__ == "__main__":
    unittest.main()
