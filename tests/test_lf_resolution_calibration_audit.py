from __future__ import annotations

import importlib.util
import math
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "scripts" / "lf_resolution_calibration_audit.py"
SPEC = importlib.util.spec_from_file_location("lf_resolution_calibration_audit", SCRIPT)
assert SPEC and SPEC.loader
MOD = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(MOD)


class LFResolutionCalibrationAuditTest(unittest.TestCase):
    def test_current_source_has_resolution_selection_defect(self) -> None:
        result = MOD.audit(ROOT)
        self.assertTrue(result["structural_defect_present"])
        contract = result["source_contract"]
        self.assertTrue(contract["discovery_active_open_only"])
        self.assertTrue(contract["closed_resolution_lookup_iterates_positions"])
        self.assertTrue(contract["resolved_scoring_uses_last_forecasts"])

    def test_non_position_forecast_is_not_in_incumbent_resolution_inventory(self) -> None:
        fixture = MOD.lifecycle_fixture()
        self.assertEqual(fixture["incumbent_resolution_lookup_ids"], ["held_good"])
        self.assertEqual(
            fixture["research_union_resolution_lookup_ids"],
            ["held_good", "unheld_bad"],
        )
        self.assertEqual(fixture["incumbent_scored_forecasts"], 1)
        self.assertEqual(fixture["research_union_scored_forecasts"], 2)

    def test_position_selected_scoring_can_bias_brier(self) -> None:
        fixture = MOD.lifecycle_fixture()
        self.assertTrue(math.isclose(fixture["incumbent_mean_brier"], 0.01, abs_tol=1e-12))
        self.assertTrue(math.isclose(fixture["all_forecast_mean_brier"], 0.41, abs_tol=1e-12))

    def test_forecast_state_cannot_support_horizon_calibration(self) -> None:
        contract = MOD.source_contract(ROOT)
        self.assertTrue(contract["forecast_state_latest_only"])
        self.assertTrue(contract["forecast_state_has_no_time_to_resolution"])


if __name__ == "__main__":
    unittest.main()
