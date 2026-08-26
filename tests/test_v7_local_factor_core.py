#!/usr/bin/env python3
from __future__ import annotations

import importlib.util
import random
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
NAME = "v7_local_factor_core"
SPEC = importlib.util.spec_from_file_location(NAME, ROOT / "scripts" / "v7_local_factor_core.py")
assert SPEC is not None and SPEC.loader is not None
lf = importlib.util.module_from_spec(SPEC)
sys.modules[NAME] = lf
SPEC.loader.exec_module(lf)


class V7LocalFactorCoreTests(unittest.TestCase):
    def test_regular_suffix_does_not_treat_gap_as_one_bar(self) -> None:
        self.assertEqual(lf.longest_regular_suffix([0, 60, 120, 480, 540, 600], 60), (480, 540, 600))

    def test_pair_factor_excludes_both_targets(self) -> None:
        times = tuple(range(20))
        c1 = [i * 0.1 for i in times]
        c2 = [i * 0.1 + (0.01 if i % 2 else -0.01) for i in times]
        a = [10.0 + x for x in c1]
        b = [-8.0 + 2.0 * x for x in c1]
        panel = lf.standardize_levels({"a": a, "b": b, "c1": c1, "c2": c2}, times)
        assert panel is not None
        fit = lf.fit_pair(panel, "a", "b")
        assert fit is not None
        self.assertEqual(fit.controls, ("c1", "c2"))

    def test_price_factor_hedge_units_counterexample(self) -> None:
        exposure_a = lf.price_factor_exposure("YES", 0.50, 0.10, 1.0)
        exposure_b = lf.price_factor_exposure("NO", 0.95, 0.30, 1.0)
        self.assertAlmostEqual(abs(exposure_a / exposure_b), 1.7543859649, places=6)

    def test_joint_state_ev_can_be_negative_with_positive_marginal_fills(self) -> None:
        distribution = lf.JointFillDistribution(0.01, 0.09, 0.09, 0.81, 100)
        value = lf.joint_execution_ev(distribution, 1.0, -0.30, -0.30)
        assert value is not None
        self.assertAlmostEqual(value.ev, -0.044, places=6)

    def test_frechet_bounds_are_not_a_joint_estimator(self) -> None:
        self.assertEqual(lf.frechet_joint_bounds(0.10, 0.10), (0.0, 0.10))

    def test_stationary_pair_is_detected_by_panel_level_null_bootstrap_fixture(self) -> None:
        rng = random.Random(4)
        n = 90
        common = [0.0]
        a = [0.0]
        b = [0.0]
        c2 = [0.0]
        e1 = e2 = 0.0
        for _ in range(1, n):
            shock = rng.gauss(0.0, 0.2)
            common.append(common[-1] + shock)
            c2.append(c2[-1] + shock + rng.gauss(0.0, 0.03))
            e1 = 0.45 * e1 + rng.gauss(0.0, 0.08)
            e2 = 0.50 * e2 + rng.gauss(0.0, 0.08)
            a.append(common[-1] + e1)
            b.append(1.30 * common[-1] + e2)
        panel = lf.standardize_levels(
            {"a": a, "b": b, "c1": common, "c2": c2}, tuple(range(n))
        )
        assert panel is not None
        output = lf.panel_pair_bootstrap_pvalues(panel, [("a", "b")], reps=79, seed=5)
        self.assertIn(("a", "b"), output)
        fit, pvalue = output[("a", "b")]
        self.assertLess(fit.pair_stat, -2.0)
        self.assertLess(pvalue, 0.25)

    def test_pair_signal_uses_price_exposure_and_ttr(self) -> None:
        rng = random.Random(7)
        n = 80
        c1 = [0.0]
        c2 = [0.0]
        a = [0.0]
        b = [0.0]
        error_a = error_b = 0.0
        for _ in range(1, n):
            shock = rng.gauss(0.0, 0.15)
            c1.append(c1[-1] + shock)
            c2.append(c2[-1] + shock + rng.gauss(0.0, 0.02))
            error_a = 0.60 * error_a + rng.gauss(0.0, 0.05)
            error_b = 0.60 * error_b + rng.gauss(0.0, 0.05)
            a.append(c1[-1] + error_a)
            b.append(c1[-1] + error_b)
        a[-1] += 1.0
        b[-1] -= 1.0
        panel = lf.standardize_levels(
            {"a": a, "b": b, "c1": c1, "c2": c2}, tuple(range(n))
        )
        assert panel is not None
        fit = lf.fit_pair(panel, "a", "b")
        assert fit is not None
        signal = lf.build_pair_signal(
            fit,
            0.02,
            {"a": 0.50, "b": 0.70},
            {"a": 0.10, "b": 0.20},
            1800,
            1000,
            {"a": 100000, "b": 100000},
            3600,
            min_abs_z=0.20,
        )
        self.assertIsNotNone(signal)
        assert signal is not None
        self.assertLessEqual(signal.hold_seconds, 24 * 3600)
        self.assertLess(signal.factor_exposure_a * signal.factor_exposure_b, 0.0)

    def test_joint_distribution_records_full_partial_and_none_states(self) -> None:
        distribution = lf.estimate_joint_distribution(
            [(True, True), (True, False), (False, True), (False, False)] * 10,
            prior=0.0,
        )
        self.assertAlmostEqual(distribution.both, 0.25)
        self.assertAlmostEqual(distribution.a_only, 0.25)
        self.assertAlmostEqual(distribution.b_only, 0.25)
        self.assertAlmostEqual(distribution.none, 0.25)
        self.assertEqual(distribution.observations, 40)


if __name__ == "__main__":
    unittest.main()
