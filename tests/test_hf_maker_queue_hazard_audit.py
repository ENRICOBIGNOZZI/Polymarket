from __future__ import annotations

import csv
import tempfile
import unittest
from pathlib import Path

from scripts.hf_maker_flow_admission import evaluate
from scripts.hf_maker_queue_hazard_audit import audit


class MakerQueueHazardAuditTest(unittest.TestCase):
    def test_quarter_touch_sizing_can_mechanically_pass_six_x_queue_gate_with_zero_flow(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            order_log = root / "maker_order_log.csv"
            tape = root / "trade_tape.csv"
            with order_log.open("w", newline="", encoding="utf-8") as handle:
                writer = csv.DictWriter(
                    handle,
                    fieldnames=[
                        "timestamp", "action", "market_id", "side", "token_id", "limit_price",
                        "remaining_shares", "queue_ahead",
                    ],
                )
                writer.writeheader()
                writer.writerow(
                    {
                        "timestamp": 100,
                        "action": "POST",
                        "market_id": "m1",
                        "side": "YES",
                        "token_id": "t1",
                        "limit_price": 0.01,
                        "remaining_shares": 250,
                        "queue_ahead": 1000,
                    }
                )
            with tape.open("w", newline="", encoding="utf-8") as handle:
                writer = csv.DictWriter(handle, fieldnames=["timestamp", "asset_id", "side", "price", "size"])
                writer.writeheader()
                writer.writerow({"timestamp": 110, "asset_id": "other", "side": "SELL", "price": 0.01, "size": 5000})

            result = audit(order_log, tape, max_queue_multiple=6.0)
            self.assertEqual(result["posted_orders"], 1)
            self.assertEqual(result["posted_with_zero_compatible_post_entry_flow"], 1)
            self.assertEqual(result["posted_near_mechanical_four_x_queue_multiple"], 1)
            self.assertTrue(result["orders"][0]["passes_queue_multiple_gate"])
            self.assertTrue(result["structural_queue_gate_problem"])
            self.assertEqual(result["decision"], "FLOW_HAZARD_REQUIRED")

    def test_causal_compatible_sell_flow_is_counted_only_after_post(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            order_log = root / "maker_order_log.csv"
            tape = root / "trade_tape.csv"
            with order_log.open("w", newline="", encoding="utf-8") as handle:
                writer = csv.DictWriter(
                    handle,
                    fieldnames=[
                        "timestamp", "action", "market_id", "side", "token_id", "limit_price",
                        "remaining_shares", "queue_ahead",
                    ],
                )
                writer.writeheader()
                writer.writerow(
                    {
                        "timestamp": 100,
                        "action": "POST",
                        "market_id": "m1",
                        "side": "YES",
                        "token_id": "t1",
                        "limit_price": 0.02,
                        "remaining_shares": 10,
                        "queue_ahead": 20,
                    }
                )
            with tape.open("w", newline="", encoding="utf-8") as handle:
                writer = csv.DictWriter(handle, fieldnames=["timestamp", "asset_id", "side", "price", "size"])
                writer.writeheader()
                writer.writerow({"timestamp": 99, "asset_id": "t1", "side": "SELL", "price": 0.01, "size": 100})
                writer.writerow({"timestamp": 101, "asset_id": "t1", "side": "BUY", "price": 0.01, "size": 100})
                writer.writerow({"timestamp": 102, "asset_id": "t1", "side": "SELL", "price": 0.03, "size": 100})
                writer.writerow({"timestamp": 103, "asset_id": "t1", "side": "SELL", "price": 0.02, "size": 7})

            result = audit(order_log, tape, max_queue_multiple=6.0)
            row = result["orders"][0]
            self.assertEqual(row["compatible_post_entry_trades"], 1)
            self.assertAlmostEqual(row["compatible_post_entry_volume"], 7.0)
            self.assertAlmostEqual(row["observed_queue_clearance_fraction"], 0.35)

    def test_positive_static_edge_cannot_admit_zero_flow_dead_queue(self) -> None:
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

    def test_inside_spread_priority_still_requires_real_contra_flow(self) -> None:
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

    def test_positive_flow_is_ranked_by_expected_filled_edge(self) -> None:
        result = evaluate(
            post_cost_edge=0.0020,
            min_edge=0.00005,
            queue_ahead=50.0,
            own_shares=10.0,
            compatible_sell_rate_per_second=0.5,
            horizon_seconds=60.0,
        )
        self.assertTrue(result.admit)
        self.assertAlmostEqual(result.fill_probability_proxy, 0.5)
        self.assertAlmostEqual(result.expected_filled_edge, 0.0010)


if __name__ == "__main__":
    unittest.main()
