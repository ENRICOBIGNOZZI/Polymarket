#!/usr/bin/env python3
from __future__ import annotations

import importlib.util
import math
from pathlib import Path
import unittest


ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location(
    "lf_v7_fdr_dependence_audit",
    ROOT / "scripts" / "lf_v7_fdr_dependence_audit.py",
)
assert SPEC and SPEC.loader
MOD = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(MOD)


class LFV7FDRDependenceAuditTest(unittest.TestCase):
    def test_current_v7_family_has_large_arbitrary_dependence_gap(self) -> None:
        report = MOD.audit(1322, 0.10)
        self.assertAlmostEqual(report["harmonic_number"], 7.7644948524570365, places=12)
        self.assertAlmostEqual(report["arbitrary_dependence_global_null_fdr"], 0.7764494852457037, places=12)
        self.assertGreater(report["arbitrary_dependence_global_null_fdr"], 7.0 * report["nominal_bh_q"])
        self.assertAlmostEqual(report["benjamini_yekutieli_effective_q"], 0.012879137909191284, places=12)

    def test_sharp_construction_has_valid_marginal_grid_pvalues(self) -> None:
        m, q = 100, 0.10
        for rank in (1, 2, 5, 10, 50, 100):
            threshold = rank * q / m
            self.assertLessEqual(MOD.marginal_grid_cdf(m, q, rank), threshold + 1e-15)

    def test_bh_rejects_every_positive_mass_event(self) -> None:
        m, q = 100, 0.10
        for k in (1, 2, 5, 10, 50, 100):
            pvalues, probability = MOD.sharp_dependence_event(m, q, k)
            self.assertGreater(probability, 0.0)
            self.assertEqual(MOD.bh_rejections(pvalues, q), k)

    def test_by_blocks_the_same_worst_case_events(self) -> None:
        m, q = 100, 0.10
        for k in (1, 2, 5, 10, 50, 100):
            pvalues, _probability = MOD.sharp_dependence_event(m, q, k)
            self.assertEqual(MOD.by_rejections(pvalues, q), 0)

    def test_worst_case_probability_equals_q_harmonic_number(self) -> None:
        m, q = 100, 0.10
        total = sum(MOD.sharp_dependence_event(m, q, k)[1] for k in range(1, m + 1))
        self.assertTrue(math.isfinite(total))
        self.assertAlmostEqual(total, q * MOD.harmonic_number(m), places=14)


if __name__ == "__main__":
    unittest.main()
