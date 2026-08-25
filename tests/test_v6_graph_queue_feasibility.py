import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from scripts.v6_graph_queue_feasibility import audit, rolling_max_flow


class GraphQueueFeasibilityTest(unittest.TestCase):
    def test_rolling_window_flow(self):
        rows = [(1_000, 10.0), (50_000, 20.0), (250_000, 30.0)]
        self.assertEqual(rolling_max_flow(rows, 180_000), 30.0)

    def test_receive_time_not_exchange_time_controls_causal_window(self):
        legs = [
            {
                "bundle_id": "b1",
                "market_id": "m1",
                "token_id": "tok",
                "limit_price": "0.40",
                "queue_ahead": "20",
                "target_shares": "10",
                "arrival_ms": "100000",
            }
        ]
        tape = [
            {
                "timestamp": "99",
                "received_ms": "101000",
                "asset_id": "tok",
                "side": "SELL",
                "price": "0.40",
                "size": "30",
            },
            {
                "timestamp": "101",
                "received_ms": "99000",
                "asset_id": "tok",
                "side": "SELL",
                "price": "0.40",
                "size": "30",
            },
        ]
        report = audit(legs, tape, lookback_seconds=10, execution_window_seconds=10)
        leg = report["bundles"][0]["legs"][0]
        self.assertEqual(leg["prior_max_execution_window_flow"], 30.0)
        self.assertEqual(leg["post_entry_execution_window_flow"], 30.0)
        self.assertEqual(leg["prior_capacity_ratio"], 1.0)
        self.assertEqual(leg["realized_clearance_ratio"], 1.0)

    def test_bundle_requires_every_leg_to_have_capacity(self):
        legs = [
            {
                "bundle_id": "b1",
                "market_id": "m1",
                "token_id": "a",
                "limit_price": "0.40",
                "queue_ahead": "20",
                "target_shares": "10",
                "arrival_ms": "100000",
            },
            {
                "bundle_id": "b1",
                "market_id": "m2",
                "token_id": "b",
                "limit_price": "0.30",
                "queue_ahead": "90",
                "target_shares": "10",
                "arrival_ms": "100000",
            },
        ]
        tape = [
            {
                "timestamp": "90",
                "received_ms": "99000",
                "asset_id": "a",
                "side": "SELL",
                "price": "0.39",
                "size": "40",
            },
            {
                "timestamp": "90",
                "received_ms": "99000",
                "asset_id": "b",
                "side": "SELL",
                "price": "0.29",
                "size": "20",
            },
        ]
        report = audit(legs, tape, lookback_seconds=10, execution_window_seconds=10)
        bundle = report["bundles"][0]
        self.assertAlmostEqual(bundle["bundle_min_prior_capacity_ratio"], 0.2)
        self.assertFalse(bundle["recent_flow_could_clear_every_leg_within_execution_window"])

    def test_wrong_side_and_above_limit_do_not_clear_queue(self):
        legs = [
            {
                "bundle_id": "b1",
                "market_id": "m1",
                "token_id": "tok",
                "limit_price": "0.40",
                "queue_ahead": "0",
                "target_shares": "10",
                "arrival_ms": "100000",
            }
        ]
        tape = [
            {"timestamp": "99", "received_ms": "99000", "asset_id": "tok", "side": "BUY", "price": "0.39", "size": "100"},
            {"timestamp": "99", "received_ms": "99500", "asset_id": "tok", "side": "SELL", "price": "0.41", "size": "100"},
        ]
        report = audit(legs, tape, lookback_seconds=10, execution_window_seconds=10)
        leg = report["bundles"][0]["legs"][0]
        self.assertEqual(leg["prior_max_execution_window_flow"], 0.0)
        self.assertEqual(leg["prior_capacity_ratio"], 0.0)


if __name__ == "__main__":
    unittest.main()
