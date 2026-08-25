from __future__ import annotations

import importlib.util
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location(
    "v6_maker_flow_audit", ROOT / "scripts" / "v6_maker_flow_audit.py"
)
MOD = importlib.util.module_from_spec(SPEC)
assert SPEC and SPEC.loader
SPEC.loader.exec_module(MOD)


def order(queue: float = 100.0, shares: float = 10.0, created: int = 1000):
    return {
        "market_id": "m1",
        "condition_id": "c1",
        "token_id": "t1",
        "side": "YES",
        "created_ts": str(created),
        "limit_price": "0.20",
        "remaining_shares": str(shares),
        "queue_ahead": str(queue),
    }


class MakerFlowAuditTest(unittest.TestCase):
    def test_uses_only_predecision_same_token_sell_through_bid(self):
        trades = [
            {"timestamp": 900, "asset": "t1", "side": "SELL", "price": 0.20, "size": 50},
            {"timestamp": 950, "asset": "t1", "side": "SELL", "price": 0.19, "size": 50},
            {"timestamp": 999, "asset": "t1", "side": "SELL", "price": 0.21, "size": 999},
            {"timestamp": 999, "asset": "other", "side": "SELL", "price": 0.19, "size": 999},
            {"timestamp": 999, "asset": "t1", "side": "BUY", "price": 0.19, "size": 999},
            {"timestamp": 1001, "asset": "t1", "side": "SELL", "price": 0.19, "size": 999},
        ]
        out = MOD.evaluate_order(order(queue=100), trades, lookback_seconds=200, ttl_seconds=30)
        self.assertEqual(out["token_sell_prints"], 3)
        self.assertEqual(out["eligible_contra_prints"], 2)
        self.assertAlmostEqual(out["eligible_contra_size"], 100.0)
        self.assertAlmostEqual(out["eligible_contra_flow_per_second"], 0.5)
        self.assertAlmostEqual(out["estimated_queue_depletion_seconds"], 200.0)
        self.assertFalse(out["fillable_within_ttl"])

    def test_marks_queue_fillable_when_predecision_flow_can_deplete_within_ttl(self):
        trades = [
            {"timestamp": 990, "asset": "t1", "side": "SELL", "price": 0.20, "size": 1000},
        ]
        out = MOD.evaluate_order(order(queue=100), trades, lookback_seconds=200, ttl_seconds=30)
        self.assertAlmostEqual(out["estimated_queue_depletion_seconds"], 20.0)
        self.assertTrue(out["fillable_within_ttl"])

    def test_zero_eligible_flow_has_no_finite_depletion_estimate(self):
        trades = [
            {"timestamp": 990, "asset": "t1", "side": "SELL", "price": 0.21, "size": 1000},
        ]
        out = MOD.evaluate_order(order(queue=100), trades, lookback_seconds=200, ttl_seconds=30)
        self.assertEqual(out["eligible_contra_prints"], 0)
        self.assertIsNone(out["estimated_queue_depletion_seconds"])
        self.assertFalse(out["fillable_within_ttl"])

    def test_queue_ratio_is_reported_for_fill_hazard_ranking(self):
        out = MOD.evaluate_order(order(queue=250, shares=10), [], lookback_seconds=300, ttl_seconds=30)
        self.assertAlmostEqual(out["queue_ratio"], 25.0)


if __name__ == "__main__":
    unittest.main()
