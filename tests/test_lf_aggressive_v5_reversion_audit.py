#!/usr/bin/env python3
from __future__ import annotations

import importlib.util
import pathlib
import sys
import unittest

ROOT = pathlib.Path(__file__).resolve().parents[1]
MODULE_PATH = ROOT / "scripts" / "lf_aggressive_v5_reversion_audit.py"
MODULE_NAME = "lf_aggressive_v5_reversion_audit"
SPEC = importlib.util.spec_from_file_location(MODULE_NAME, MODULE_PATH)
assert SPEC and SPEC.loader
MODULE = importlib.util.module_from_spec(SPEC)
sys.modules[MODULE_NAME] = MODULE
SPEC.loader.exec_module(MODULE)


class AggressiveV5ReversionAuditTest(unittest.TestCase):
    def test_aggressive_gate_is_strictly_relaxed(self) -> None:
        incumbent = MODULE.INCUMBENT
        aggressive = MODULE.AGGRESSIVE_V5
        self.assertLess(aggressive.min_t, incumbent.min_t)
        self.assertLess(aggressive.min_stability, incumbent.min_stability)
        self.assertLess(aggressive.min_series, incumbent.min_series)
        self.assertLess(aggressive.min_transitions, incumbent.min_transitions)
        self.assertLess(aggressive.min_half, incumbent.min_half)
        self.assertGreater(aggressive.max_half_life_hours, incumbent.max_half_life_hours)
        self.assertEqual(aggressive.min_t, 0.60)
        self.assertEqual(aggressive.max_half_life_hours, 336.0)

    def test_unit_root_false_admission_rises_materially_at_48_points(self) -> None:
        rows = MODULE.summarize(200)["experiments"]
        iid = next(
            row for row in rows
            if row["process"] == "unit_root" and row["length"] == 48 and row["innovation_rho"] == 0.0
        )
        serial = next(
            row for row in rows
            if row["process"] == "unit_root" and row["length"] == 48 and row["innovation_rho"] == 0.65
        )
        self.assertEqual(iid["incumbent_pass"], 62)
        self.assertEqual(iid["aggressive_pass"], 148)
        self.assertEqual(serial["incumbent_pass"], 19)
        self.assertEqual(serial["aggressive_pass"], 77)
        self.assertGreater(iid["aggressive_rate"], 0.70)
        self.assertGreater(serial["aggressive_rate"], 0.35)

    def test_relaxation_remains_large_on_long_paths(self) -> None:
        rows = MODULE.summarize(200)["experiments"]
        iid = next(
            row for row in rows
            if row["process"] == "unit_root" and row["length"] == 672 and row["innovation_rho"] == 0.0
        )
        serial = next(
            row for row in rows
            if row["process"] == "unit_root" and row["length"] == 672 and row["innovation_rho"] == 0.65
        )
        stationary = next(
            row for row in rows
            if row["process"] == "stationary_ar1" and row["length"] == 672
        )
        self.assertEqual(iid["incumbent_pass"], 79)
        self.assertEqual(iid["aggressive_pass"], 163)
        self.assertEqual(serial["incumbent_pass"], 26)
        self.assertEqual(serial["aggressive_pass"], 68)
        self.assertEqual(stationary["incumbent_pass"], 200)
        self.assertEqual(stationary["aggressive_pass"], 200)

    def test_evidence_is_bound_to_current_aggressive_source_head(self) -> None:
        report = MODULE.summarize(10)
        self.assertEqual(report["source_research_pr"], 154)
        self.assertEqual(report["source_research_head"], "8b39ceb5182432def738ffdee2d2c7ba8c5567f1")
        self.assertEqual(report["integration_pr"], 156)
        self.assertEqual(report["decision"], "MORE_EVIDENCE_REQUIRED")


if __name__ == "__main__":
    unittest.main()
