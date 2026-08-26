from __future__ import annotations

import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from scripts.lf_v6_history_spacing_audit import half_life_steps, source_contract, spacing_case


class LocalFactorHistorySpacingTest(unittest.TestCase):
    def test_regular_hourly_sampling_preserves_hourly_phi(self) -> None:
        case = spacing_case(0.95, 1.0)
        self.assertAlmostEqual(case.estimated_one_step_phi, 0.95, places=12)
        self.assertAlmostEqual(case.modeled_half_life_hours, half_life_steps(0.95), places=10)

    def test_six_hour_gaps_are_misread_as_one_hour_steps(self) -> None:
        case = spacing_case(0.95, 6.0)
        self.assertAlmostEqual(case.estimated_one_step_phi, 0.95**6, places=12)
        self.assertAlmostEqual(case.modeled_half_life_hours, case.true_half_life_hours / 6.0, places=10)
        self.assertGreater(case.reversion_overstatement, 3.5)

    def test_twelve_hour_gaps_make_forecast_error_larger(self) -> None:
        case = spacing_case(0.95, 12.0)
        self.assertAlmostEqual(case.estimated_one_step_phi, 0.95**12, places=12)
        self.assertGreater(case.reversion_overstatement, 6.0)

    def test_current_source_does_not_validate_adjacent_spacing(self) -> None:
        contract = source_contract(ROOT / "scripts" / "v6_local_factor_intents.py")
        self.assertTrue(contract["intersects_timestamps"])
        self.assertTrue(contract["fits_ar_without_timestamps"])
        self.assertTrue(contract["uses_configured_fidelity_for_hold"])
        self.assertFalse(contract["checks_adjacent_timestamp_spacing"])


if __name__ == "__main__":
    unittest.main()
