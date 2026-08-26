from __future__ import annotations

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str((Path(__file__).resolve().parents[1] / "scripts").resolve()))

from hf_fill_conditioned_maker_router import CandidateEvidence, RouterConfig, evaluate


def candidate(**overrides):
    values = dict(
        post_cost_edge=0.0100,
        tick_size=0.0010,
        queue_ahead=20.0,
        own_shares=10.0,
        recent_trade_count=6,
        compatible_sell_prints=3,
        compatible_sell_volume=15.0,
        recent_buy_volume=30.0,
        recent_sell_volume=20.0,
        at_touch_fill_probability=0.15,
        at_touch_markout_per_share=0.0010,
        inside_fill_probability=0.30,
        inside_markout_per_share=-0.0010,
        capital_latency_cost_per_share=0.0001,
    )
    values.update(overrides)
    return CandidateEvidence(**values)


class FillConditionedMakerRouterTest(unittest.TestCase):
    def test_rejects_static_edge_without_recent_activity(self):
        d = evaluate(candidate(recent_trade_count=0, compatible_sell_prints=0, compatible_sell_volume=0.0))
        self.assertEqual(d.action, "SKIP")
        self.assertEqual(d.reason, "insufficient_recent_activity")

    def test_rejects_tiny_contra_flow_behind_large_queue(self):
        d = evaluate(candidate(queue_ahead=67.4, own_shares=16.85, compatible_sell_prints=1, compatible_sell_volume=0.02))
        self.assertEqual(d.action, "SKIP")
        self.assertEqual(d.reason, "recent_flow_cannot_clear_queue")
        self.assertLess(d.recent_clearance_ratio, 0.001)

    def test_rejects_recurrent_sell_toxicity(self):
        d = evaluate(candidate(recent_trade_count=12, recent_buy_volume=1.0, recent_sell_volume=99.0))
        self.assertEqual(d.action, "SKIP")
        self.assertEqual(d.reason, "directional_sell_flow_too_toxic")

    def test_sparse_flow_never_authorizes_inside_improvement(self):
        d = evaluate(candidate(recent_trade_count=2, compatible_sell_prints=1, recent_buy_volume=10.0, recent_sell_volume=5.0))
        self.assertEqual(d.action, "POST_AT_TOUCH")

    def test_inside_improvement_requires_incremental_ev_after_markout(self):
        d = evaluate(candidate(
            at_touch_fill_probability=0.10,
            at_touch_markout_per_share=0.001,
            inside_fill_probability=0.50,
            inside_markout_per_share=-0.0005,
        ))
        self.assertEqual(d.action, "IMPROVE_ONE_TICK")
        self.assertGreater(d.inside_ev_per_share, d.at_touch_ev_per_share)

    def test_negative_inside_markout_blocks_improvement_even_with_more_fills(self):
        d = evaluate(candidate(
            at_touch_fill_probability=0.10,
            at_touch_markout_per_share=0.001,
            inside_fill_probability=0.70,
            inside_markout_per_share=-0.020,
        ))
        self.assertEqual(d.action, "POST_AT_TOUCH")
        self.assertLess(d.inside_ev_per_share, d.at_touch_ev_per_share)

    def test_nonpositive_fill_conditioned_touch_ev_is_rejected(self):
        d = evaluate(candidate(at_touch_markout_per_share=-0.020, inside_markout_per_share=-0.020))
        self.assertEqual(d.action, "SKIP")
        self.assertEqual(d.reason, "nonpositive_touch_fill_conditioned_ev")

    def test_authorized_floor_is_not_lowered(self):
        cfg = RouterConfig()
        self.assertEqual(cfg.min_post_cost_edge, 0.00005)
        d = evaluate(candidate(post_cost_edge=0.000049))
        self.assertEqual(d.reason, "post_cost_edge_below_floor")


if __name__ == "__main__":
    unittest.main()
