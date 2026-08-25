#!/usr/bin/env python3
from __future__ import annotations

import importlib.util
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location(
    "lf_v6_unit_root_bootstrap_audit",
    ROOT / "scripts" / "lf_v6_unit_root_bootstrap_audit.py",
)
MODULE = importlib.util.module_from_spec(SPEC)
assert SPEC and SPEC.loader
SPEC.loader.exec_module(MODULE)


class LFV6UnitRootBootstrapAuditTest(unittest.TestCase):
    def _null_rates(self, length: int, rho: float) -> tuple[float, float]:
        paths = 40
        reps = 120
        candidate_rejections = 0
        robust_rejections = 0
        for index in range(paths):
            seed = 91_000_000 + length * 1000 + int(rho * 100) * 100 + index
            levels = MODULE.random_walk(length, rho, seed)
            if MODULE.score_centered_block_pvalue(levels, seed + 101, reps) <= 0.10:
                candidate_rejections += 1
            if MODULE.unit_root_block_adf_pvalue(levels, seed + 202, reps) <= 0.10:
                robust_rejections += 1
        return candidate_rejections / paths, robust_rejections / paths

    def test_iid_unit_root_score_has_mechanical_negative_expectation(self) -> None:
        self.assertAlmostEqual(MODULE.iid_unit_root_score_expectation(1.0), -0.5)
        self.assertAlmostEqual(MODULE.iid_unit_root_score_expectation(4.0), -2.0)

    def test_score_centered_bootstrap_is_not_unit_root_calibrated(self) -> None:
        candidate_rate, robust_rate = self._null_rates(96, 0.0)
        self.assertGreater(candidate_rate, 0.50)
        self.assertLessEqual(robust_rate, 0.20)

    def test_null_preserving_bootstrap_handles_serial_increment_dependence(self) -> None:
        candidate_rate, robust_rate = self._null_rates(96, 0.5)
        self.assertGreater(candidate_rate, 0.25)
        self.assertLessEqual(robust_rate, 0.20)

    def test_null_preserving_adf_bootstrap_retains_long_history_power(self) -> None:
        paths = 30
        reps = 120
        rejected = 0
        for index in range(paths):
            seed = 92_000_000 + 336 * 1000 + 900 + index
            levels = MODULE.stationary_ar1(336, 0.90, seed)
            if MODULE.unit_root_block_adf_pvalue(levels, seed + 202, reps) <= 0.10:
                rejected += 1
        self.assertGreaterEqual(rejected / paths, 0.80)

    def test_experiment_is_reproducible_and_fail_closed(self) -> None:
        result = MODULE.run_experiment(paths=12, reps=60)
        self.assertEqual(result["decision"], "MORE_EVIDENCE_REQUIRED")
        self.assertEqual(len(result["null_results"]), 6)
        self.assertEqual(result["alpha"], 0.10)
        self.assertEqual(result["analytic_iid_unit_root_score_expectation_variance_1"], -0.5)


if __name__ == "__main__":
    unittest.main()
