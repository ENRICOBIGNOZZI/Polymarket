#!/usr/bin/env python3
from __future__ import annotations

import importlib.util
import math
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location(
    "lf_v6_joint_completion_dependence_audit",
    ROOT / "scripts" / "lf_v6_joint_completion_dependence_audit.py",
)
assert SPEC is not None and SPEC.loader is not None
MODULE = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(MODULE)


class JointCompletionDependenceAuditTest(unittest.TestCase):
    def test_frechet_bounds_show_independence_is_not_identified(self) -> None:
        marginals = [0.02] * 5
        lower, upper = MODULE.frechet_joint_bounds(marginals)
        independent = MODULE.independent_joint_probability(marginals)
        self.assertEqual(lower, 0.0)
        self.assertAlmostEqual(upper, 0.02)
        self.assertAlmostEqual(independent, 3.2e-9)
        self.assertGreater(upper / independent, 6_000_000.0)

    def test_same_marginals_support_opposite_joint_completion(self) -> None:
        evidence = MODULE.same_marginal_counterexample()
        self.assertEqual(evidence["marginals"], [0.02] * 5)
        self.assertAlmostEqual(evidence["common_shock_empirical_joint"], 0.02)
        self.assertEqual(evidence["exclusive_empirical_joint"], 0.0)
        self.assertGreater(evidence["common_shock_ev_usd"], 0.0)
        self.assertLess(evidence["exclusive_ev_usd"], 0.0)

    def test_same_window_queue_and_target_size_determine_completion_state(self) -> None:
        legs = [
            MODULE.LegRequirement("A", 0.40, queue_ahead=8.0, own_shares=2.0),
            MODULE.LegRequirement("B", 0.30, queue_ahead=4.0, own_shares=1.0),
        ]
        trades = [
            MODULE.TapeTrade(101, "A", "SELL", 0.40, 6.0),
            MODULE.TapeTrade(102, "B", "SELL", 0.29, 5.0),
            MODULE.TapeTrade(103, "A", "SELL", 0.39, 4.0),
        ]
        state = MODULE.completion_state_for_window(trades, legs, start_ts=100, end_ts=110)
        self.assertEqual(state, (True, True))

        smaller_flow = trades[:-1]
        state = MODULE.completion_state_for_window(smaller_flow, legs, start_ts=100, end_ts=110)
        self.assertEqual(state, (False, True))

    def test_windowing_preserves_cross_leg_timing(self) -> None:
        legs = [
            MODULE.LegRequirement("A", 0.50, queue_ahead=0.0, own_shares=1.0),
            MODULE.LegRequirement("B", 0.50, queue_ahead=0.0, own_shares=1.0),
        ]
        trades = [
            MODULE.TapeTrade(101, "A", "SELL", 0.50, 1.0),
            MODULE.TapeTrade(111, "B", "SELL", 0.50, 1.0),
        ]
        states = MODULE.rolling_completion_states(
            trades,
            legs,
            start_ts=100,
            end_ts=120,
            window_seconds=10,
        )
        self.assertEqual(states, [(True, False), (False, True)])
        self.assertEqual(MODULE.empirical_joint_probability(states), 0.0)
        self.assertAlmostEqual(MODULE.independent_joint_probability(MODULE.empirical_marginals(states)), 0.25)


if __name__ == "__main__":
    unittest.main()
