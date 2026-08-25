#!/usr/bin/env python3
from __future__ import annotations

import importlib.util
import math
import sys
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location(
    "lf_v6_ttr_horizon_audit", ROOT / "scripts" / "lf_v6_ttr_horizon_audit.py"
)
assert SPEC is not None and SPEC.loader is not None
MOD = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = MOD
SPEC.loader.exec_module(MOD)


class V6TimeToResolutionHorizonAuditTest(unittest.TestCase):
    def test_current_source_has_no_ttr_binding(self) -> None:
        source = (ROOT / "scripts" / "v6_local_factor_intents.py").read_text(encoding="utf-8")
        contract = MOD.source_contract(source)
        self.assertFalse(contract["market_has_expiry_or_resolution_metadata"])
        self.assertFalse(contract["build_pair_intent_receives_expiry_or_ttr"])
        self.assertFalse(contract["build_pair_intent_receives_fidelity"])
        self.assertTrue(contract["hold_is_two_half_lives_capped_24_then_times_3600"])
        self.assertTrue(contract["candidate_uses_one_step_ar_change"])

    def test_two_hour_resolution_is_beyond_incumbent_exit(self) -> None:
        row = MOD.evaluate(MOD.HorizonCase(phi=0.90, fidelity_minutes=60, time_to_resolution_hours=2.0))
        self.assertAlmostEqual(row["half_life_bars"], 6.57881347896, places=9)
        self.assertAlmostEqual(row["incumbent_hold_hours"], 13.15762695792, places=9)
        self.assertGreater(row["incumbent_exit_after_resolution_hours"], 11.0)
        self.assertAlmostEqual(row["guarded_hold_hours"], 1.75, places=12)

    def test_guarded_forecast_matches_actual_horizon(self) -> None:
        row = MOD.evaluate(MOD.HorizonCase(phi=0.90, fidelity_minutes=60, time_to_resolution_hours=6.0))
        self.assertAlmostEqual(row["guarded_hold_hours"], 5.75, places=12)
        expected = 0.90 ** 5.75 - 1.0
        self.assertAlmostEqual(row["horizon_matched_change_per_unit_deviation"], expected, places=12)
        self.assertGreater(abs(expected), abs(row["one_step_residual_change_per_unit_deviation"]))

    def test_fidelity_changes_wall_clock_half_life(self) -> None:
        hourly = MOD.natural_hold_hours(0.90, 60)
        half_hour = MOD.natural_hold_hours(0.90, 30)
        self.assertAlmostEqual(half_hour, 0.5 * hourly, places=12)
        self.assertTrue(math.isclose(MOD.incumbent_hold_hours(0.90), hourly, rel_tol=0.0, abs_tol=1e-12))

    def test_abstain_if_no_pre_resolution_markout_window(self) -> None:
        case = MOD.HorizonCase(
            phi=0.90,
            fidelity_minutes=60,
            time_to_resolution_hours=0.60,
            exit_buffer_hours=0.25,
            min_hold_hours=0.50,
        )
        self.assertIsNone(MOD.guarded_hold_hours(case))
        self.assertEqual(MOD.evaluate(case)["decision"], "ABSTAIN_TTR_TOO_SHORT")


if __name__ == "__main__":
    unittest.main()
