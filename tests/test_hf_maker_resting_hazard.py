from __future__ import annotations

import unittest

from scripts.hf_maker_resting_hazard_replay import replay_post


class RestingHazardReplayTest(unittest.TestCase):
    def test_stale_hazard_cancels_without_lookahead_and_saves_capital(self) -> None:
        post = {
            "timestamp": "100", "action": "POST", "market_id": "m", "side": "YES",
            "token_id": "t", "limit_price": "0.4", "remaining_shares": "10",
            "queue_ahead": "10", "fill_probability": "0.9", "flow_rate": "3",
        }
        orders = [post, {**post, "timestamp": "160", "action": "CANCEL_TTL"}]
        tape = [{"timestamp": "95", "received_ms": "99500", "asset_id": "t", "side": "SELL", "price": "0.4", "size": "50"}]
        result = replay_post(post, orders, tape, ttl_seconds=60, grace_seconds=20, stale_receive_seconds=20, revalidate_seconds=5)
        self.assertEqual(result["dynamic_end_ts"], 120)
        self.assertEqual(result["dynamic_outcome"], "CANCEL_STALE_RESTING_HAZARD")
        self.assertFalse(result["missed_fill_due_dynamic_cancel"])
        self.assertGreater(result["capital_seconds_saved"], 0.0)

    def test_fill_before_stale_cancel_is_preserved(self) -> None:
        post = {
            "timestamp": "100", "action": "POST", "market_id": "m", "side": "YES",
            "token_id": "t", "limit_price": "0.4", "remaining_shares": "10",
            "queue_ahead": "10", "fill_probability": "0.4", "flow_rate": "1",
        }
        orders = [post]
        tape = [
            {"timestamp": "95", "received_ms": "99500", "asset_id": "t", "side": "SELL", "price": "0.4", "size": "10"},
            {"timestamp": "110", "received_ms": "110500", "asset_id": "t", "side": "SELL", "price": "0.4", "size": "25"},
        ]
        result = replay_post(post, orders, tape, ttl_seconds=60, grace_seconds=20, stale_receive_seconds=20, revalidate_seconds=5)
        self.assertTrue(result["static_fill_before_end"])
        self.assertTrue(result["dynamic_fill_before_cancel"])
        self.assertFalse(result["missed_fill_due_dynamic_cancel"])

    def test_delayed_fill_after_causal_cancel_is_counted_as_missed_fill(self) -> None:
        post = {
            "timestamp": "100", "action": "POST", "market_id": "m", "side": "YES",
            "token_id": "t", "limit_price": "0.4", "remaining_shares": "10",
            "queue_ahead": "10", "fill_probability": "0.8", "flow_rate": "2",
        }
        orders = [post]
        tape = [
            {"timestamp": "95", "received_ms": "99500", "asset_id": "t", "side": "SELL", "price": "0.4", "size": "10"},
            {"timestamp": "130", "received_ms": "130500", "asset_id": "t", "side": "SELL", "price": "0.4", "size": "25"},
        ]
        result = replay_post(post, orders, tape, ttl_seconds=60, grace_seconds=20, stale_receive_seconds=20, revalidate_seconds=5)
        self.assertTrue(result["static_fill_before_end"])
        self.assertFalse(result["dynamic_fill_before_cancel"])
        self.assertTrue(result["missed_fill_due_dynamic_cancel"])

    def test_queue_only_flow_is_not_misclassified_as_fill(self) -> None:
        post = {
            "timestamp": "100", "action": "POST", "market_id": "m", "side": "YES",
            "token_id": "t", "limit_price": "0.4", "remaining_shares": "10",
            "queue_ahead": "5", "fill_probability": "0.8", "flow_rate": "2",
        }
        orders = [post]
        tape = [
            {"timestamp": "95", "received_ms": "99500", "asset_id": "t", "side": "SELL", "price": "0.4", "size": "10"},
            {"timestamp": "110", "received_ms": "110500", "asset_id": "t", "side": "SELL", "price": "0.4", "size": "5"},
        ]
        result = replay_post(post, orders, tape, ttl_seconds=60, grace_seconds=20, stale_receive_seconds=20, revalidate_seconds=5)
        self.assertTrue(result["queue_cleared_before_static_end"])
        self.assertFalse(result["own_size_cleared_before_static_end"])
        self.assertFalse(result["static_fill_before_end"])

    def test_late_received_pre_post_event_cannot_fill_order(self) -> None:
        post = {
            "timestamp": "100", "action": "POST", "market_id": "m", "side": "YES",
            "token_id": "t", "limit_price": "0.4", "remaining_shares": "10",
            "queue_ahead": "0", "fill_probability": "0.8", "flow_rate": "2",
        }
        orders = [post]
        tape = [{"timestamp": "95", "received_ms": "110500", "asset_id": "t", "side": "SELL", "price": "0.4", "size": "100"}]
        result = replay_post(post, orders, tape, ttl_seconds=60, grace_seconds=20, stale_receive_seconds=20, revalidate_seconds=5)
        self.assertFalse(result["static_fill_before_end"])
        self.assertFalse(result["dynamic_fill_before_cancel"])


if __name__ == "__main__":
    unittest.main()
