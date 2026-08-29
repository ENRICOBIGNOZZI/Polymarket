from __future__ import annotations

import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = ROOT / "scripts"
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))

import v7_local_factor_multiplicity as mult


class LocalFactorMultiplicityTest(unittest.TestCase):
    def test_harmonic_and_effective_q(self) -> None:
        self.assertAlmostEqual(mult.harmonic_number(3), 1.0 + 0.5 + 1.0 / 3.0)
        self.assertAlmostEqual(mult.by_effective_q(0.10, 3), 0.10 / mult.harmonic_number(3))

    def test_by_is_more_conservative_than_bh_fixture(self) -> None:
        # At m=3 and q=.10, BH first cutoff is .0333 while BY first cutoff is
        # about .01818. The first p-value passes BH but correctly fails BY.
        pvalues = {"a": 0.025, "b": 0.50, "c": 1.0}
        self.assertEqual(mult.by_selected(pvalues, 0.10), set())
        stronger = {"a": 0.01, "b": 0.50, "c": 1.0}
        self.assertEqual(mult.by_selected(stronger, 0.10), {"a"})

    def test_missing_predeclared_hypotheses_remain_p_one(self) -> None:
        complete = {"estimated": 0.02, "missing_1": 1.0, "missing_2": 1.0}
        reduced = {"estimated": 0.02}
        self.assertEqual(mult.by_selected(complete, 0.10), set())
        self.assertEqual(mult.by_selected(reduced, 0.10), {"estimated"})

    def test_203_pair_family_needs_about_twelve_thousand_reps(self) -> None:
        diag = mult.by_resolution_diagnostics(203, 5000, 0.10)
        self.assertFalse(diag["singleton_by_resolution_adequate"])
        self.assertGreaterEqual(diag["repetitions_required_for_singleton_by_resolution"], 11900)
        self.assertLessEqual(diag["repetitions_required_for_singleton_by_resolution"], 12100)
        enough = mult.by_resolution_diagnostics(
            203,
            int(diag["repetitions_required_for_singleton_by_resolution"]),
            0.10,
        )
        self.assertTrue(enough["singleton_by_resolution_adequate"])


if __name__ == "__main__":
    unittest.main()
