from __future__ import annotations

import unittest

from scripts.hf_maker_flow_admission import evaluate


class MakerFlowAdmissionTest(unittest.TestCase):
    def test_positive_static_edge_does_not_admit_zero_flow_dead_queue(self) -> None:
        result = evaluate(
            post_cost_edge=0.00030,
            min_edge=0.00005,
            queue_ahead=26099.9,
            own_shares=6524.98,
            compatible_sell_rate_per_second=0.0,
            horizon_seconds=60.0,
        )
        self.assertFalse(result.admit)
        self.assertEqual(result.reason, "ZERO_CAUSAL_CONTRA_FLOW")
        self.assertEqual(result.expected_filled_edge, 0.0)

    def test_inside_spread_still_requires_actual_contra_flow(self) -> None:
        result = evaluate(
            post_cost_edge=0.0010,
            min_edge=0.00005,
            queue_ahead=0.0,
            own_shares=20.0,
            compatible_sell_rate_per_second=0.0,
            horizon_seconds=60.0,
            inside_spread=True,
        )
        self.assertFalse(result.admit)
        self.assertEqual(result.reason, "ZERO_CAUSAL_CONTRA_FLOW")

    def test_positive_flow_candidate_is_ranked_by_expected_filled_edge(self) -> None:
        result = evaluate(
            post_cost_edge=0.0020,
            min_edge=0.00005,
            queue_ahead=50.0,
            own_shares=10.0,
            compatible_sell_rate_per_second=0.5,
            horizon_seconds=60.0,
        )
        self.assertTrue(result.admit)
        self.assertEqual(result.reason, "POSITIVE_CAUSAL_FLOW_RANK")
        self.assertAlmostEqual(result.queue_clearance_ratio, 0.5)
        self.assertAlmostEqual(result.fill_probability_proxy, 0.5)
        self.assertAlmostEqual(result.expected_filled_edge, 0.0010)

    def test_edge_floor_is_checked_before_fill_hazard(self) -> None:
        result = evaluate(
            post_cost_edge=0.00001,
            min_edge=0.00005,
            queue_ahead=0.0,
            own_shares=10.0,
            compatible_sell_rate_per_second=100.0,
            horizon_seconds=60.0,
            inside_spread=True,
        )
        self.assertFalse(result.admit)
        self.assertEqual(result.reason, "EDGE_BELOW_FLOOR")


if __name__ == "__main__":
    unittest.main()
