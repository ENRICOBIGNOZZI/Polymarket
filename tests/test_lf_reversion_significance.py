#!/usr/bin/env python3
from __future__ import annotations

import importlib.util
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location(
    "lf_reversion_significance", ROOT / "scripts" / "lf_reversion_significance.py"
)
assert SPEC and SPEC.loader
lf = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = lf
SPEC.loader.exec_module(lf)


class LFReversionSignificanceTests(unittest.TestCase):
    def test_unit_root_fixture_is_false_positive_under_current_threshold(self) -> None:
        result = lf.diagnose_reversion(
            lf.synthetic_unit_root(), reps=399, block_length=16, seed=773
        )
        self.assertTrue(result.incumbent_pass)
        self.assertFalse(result.bootstrap_pass)
        self.assertLess(result.iid_t, -1.75)
        self.assertGreater(result.iid_t, result.bootstrap_critical_5pct)

    def test_stationary_positive_control_survives_bootstrap_gate(self) -> None:
        result = lf.diagnose_reversion(
            lf.synthetic_stationary(), reps=399, block_length=16, seed=773
        )
        self.assertTrue(result.incumbent_pass)
        self.assertTrue(result.bootstrap_pass)
        self.assertLessEqual(result.iid_t, result.bootstrap_critical_5pct)

    def test_full_incumbent_gate_has_material_unit_root_false_positive_rate(self) -> None:
        serial = lf.unit_root_gate_false_positive_rate(paths=200, innovation_rho=0.65)
        iid = lf.unit_root_gate_false_positive_rate(paths=200, innovation_rho=0.0)
        self.assertEqual(serial["passed"], 29)
        self.assertAlmostEqual(serial["rate"], 0.145)
        self.assertEqual(iid["passed"], 83)
        self.assertAlmostEqual(iid["rate"], 0.415)

    def test_bootstrap_is_deterministic(self) -> None:
        values = lf.synthetic_unit_root(seed=42)
        first = lf.diagnose_reversion(values, reps=199, block_length=12, seed=123)
        second = lf.diagnose_reversion(values, reps=199, block_length=12, seed=123)
        self.assertEqual(first, second)

    def test_production_b2_uses_iid_se_and_standard_style_cutoff(self) -> None:
        source = (ROOT / "src" / "pca_stat_arb.cpp").read_text(encoding="utf-8")
        self.assertIn("sigma2) / sxx", source)
        self.assertIn("min_t = 1.75", source)
        self.assertIn("fit.t_reversion > -min_t", source)


if __name__ == "__main__":
    unittest.main()
