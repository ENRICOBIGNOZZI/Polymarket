#!/usr/bin/env python3
from __future__ import annotations

import importlib.util
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
MODULE_PATH = ROOT / "scripts" / "lf_v6_pair_completion_state_audit.py"
SPEC = importlib.util.spec_from_file_location("lf_pair_completion_audit", MODULE_PATH)
assert SPEC is not None and SPEC.loader is not None
MODULE = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(MODULE)


class LocalFactorPairCompletionAuditTest(unittest.TestCase):
    def test_min_marginal_is_frechet_upper_bound_not_joint_estimate(self) -> None:
        lower, upper = MODULE.frechet_joint_bounds(0.10, 0.10)
        self.assertAlmostEqual(lower, 0.0)
        self.assertAlmostEqual(upper, 0.10)
        self.assertAlmostEqual(MODULE.incumbent_completion_proxy(0.10, 0.10), upper)
        self.assertAlmostEqual(0.10 * 0.10, 0.01)

    def test_same_marginals_allow_zero_or_ten_percent_completion(self) -> None:
        mutually_exclusive = MODULE.fill_state_probabilities(0.10, 0.10, 0.0)
        perfectly_dependent = MODULE.fill_state_probabilities(0.10, 0.10, 0.10)
        self.assertAlmostEqual(mutually_exclusive["both"], 0.0)
        self.assertAlmostEqual(mutually_exclusive["a_only"], 0.10)
        self.assertAlmostEqual(mutually_exclusive["b_only"], 0.10)
        self.assertAlmostEqual(perfectly_dependent["both"], 0.10)
        self.assertAlmostEqual(perfectly_dependent["a_only"], 0.0)
        self.assertAlmostEqual(perfectly_dependent["b_only"], 0.0)

    def test_partial_fill_unwind_can_flip_ev_sign(self) -> None:
        economics = MODULE.PairEconomics(
            p_a=0.10,
            p_b=0.10,
            completed_pnl=1.00,
            a_only_pnl=-0.30,
            b_only_pnl=-0.30,
        )
        ev_mutually_exclusive = MODULE.expected_pair_pnl(economics, 0.0)
        ev_independent = MODULE.expected_pair_pnl(economics, 0.01)
        ev_perfect_dependence = MODULE.expected_pair_pnl(economics, 0.10)
        self.assertAlmostEqual(ev_mutually_exclusive, -0.06)
        self.assertAlmostEqual(ev_independent, -0.044)
        self.assertAlmostEqual(ev_perfect_dependence, 0.10)
        self.assertLess(ev_mutually_exclusive, 0.0)
        self.assertGreater(ev_perfect_dependence, 0.0)

    def test_invalid_joint_is_rejected(self) -> None:
        with self.assertRaises(ValueError):
            MODULE.fill_state_probabilities(0.10, 0.10, 0.11)

    def test_frozen_audit_exposes_positive_proxy_with_negative_state_ev(self) -> None:
        result = MODULE.audit_case()
        joint = result["joint_completion"]
        scenarios = result["scenarios"]
        self.assertTrue(joint["incumbent_is_upper_bound"])
        self.assertGreater(joint["incumbent_min_marginal_proxy"], 0.0)
        self.assertLess(scenarios["mutually_exclusive"]["expected_pnl_per_window"], 0.0)
        self.assertGreater(scenarios["perfect_positive_dependence"]["expected_pnl_per_window"], 0.0)


if __name__ == "__main__":
    unittest.main()
