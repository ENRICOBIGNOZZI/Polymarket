from pathlib import Path
import sys
import unittest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

from v7_alpha_driven_maker_shadow import make_anchor, make_protocol, replay_anchor_python


class AlphaDrivenMakerShadowTests(unittest.TestCase):
    def test_protocol_grid_is_bounded_and_deterministic(self):
        p = make_protocol([1000, 250, 500, 250], [5000, 250, 1000])
        arms = p["maker"]["arms"]
        self.assertEqual(
            [a["id"] for a in arms],
            [
                "JOIN_250MS", "JOIN_500MS", "JOIN_1000MS",
                "IMPROVE1_250MS", "IMPROVE1_500MS", "IMPROVE1_1000MS",
            ],
        )
        self.assertEqual(p["maker"]["markout_horizons_ms"], [250, 1000, 5000])

    def test_anchor_is_zero_authority_counterfactual(self):
        row = {
            "best_bid": 0.49,
            "receive_wall_ms": 123456,
            "observer_sequence": 77,
            "receive_monotonic_ns": 999_000_000,
            "exchange_event_ns": 888_000_000,
        }
        anchor = make_anchor(
            row,
            market_id="m1",
            token_id="t1",
            model_sha="a" * 40,
            quantity=5.0,
            opportunity=None,
        )
        order = anchor["order"]
        self.assertTrue(order["paper_only"])
        self.assertFalse(order["authenticated_execution"])
        self.assertFalse(order["real_order_submission"])
        self.assertTrue(order["metadata"]["counterfactual"])
        self.assertTrue(order["metadata"]["excluded_from_portfolio_equity"])
        self.assertEqual(order["intended_size"], 5.0)
        self.assertEqual(order["limit_price"], 0.49)


    def test_pessimistic_queue_and_improve1_replay(self):
        import time
        from types import SimpleNamespace

        sha = "a" * 40
        origin = {
            "best_bid": 0.49, "best_ask": 0.51, "tick_size": 0.01,
            "bid_depth_l1": 2.0, "receive_wall_ms": 1000,
            "receive_monotonic_ns": 1_000_000_000,
            "exchange_event_ns": 900_000_000,
            "observer_sequence": 1, "valid": True, "lineage_continuous": True,
            "public_trade": None,
        }
        trade = {
            **origin,
            "receive_wall_ms": 1050,
            "receive_monotonic_ns": 1_050_000_000,
            "exchange_event_ns": 950_000_000,
            "observer_sequence": 2,
            "public_trade": {
                "aggressor_side": "SELL", "price": 0.49, "size": 4.0,
                "exchange_event_ns": 950_000_000,
            },
        }
        mark = {
            **origin,
            "best_bid": 0.50, "best_ask": 0.52,
            "receive_wall_ms": 1100,
            "receive_monotonic_ns": 1_100_000_000,
            "exchange_event_ns": 1_000_000_000,
            "observer_sequence": 3,
            "public_trade": None,
        }
        book = SimpleNamespace(
            history={("m1", "t1"): [origin, trade, mark]},
            model_sha=sha, gaps=0, session="s", epoch=1, sequence=3,
            watermark_ms=1200, watermark_monotonic_ns=1_200_000_000,
        )
        anchor = make_anchor(
            origin, market_id="m1", token_id="t1", model_sha=sha,
            quantity=5.0, opportunity=None,
        )
        anchor["book_gap_counter"] = 0
        anchor["observer_session_id"] = "s"
        anchor["connection_epoch"] = 1
        status = {
            "timestamp_ms": time.time_ns() // 1_000_000,
            "book_events_written": 3,
            "book_watermark_receive_wall_ms": 1200,
            "book_watermark_receive_monotonic_ns": 1_200_000_000,
            "evidence_complete": True, "model_sha": sha,
            "observer_session_id": "s", "connection_epoch": 1,
            "state": "running", "paper_only": True,
            "authenticated_execution": False, "real_order_submission": False,
        }
        result = replay_anchor_python(
            anchor, book, status, make_protocol([50], [50]), evaluation_ms=100
        )
        by = {row["arm"]: row for row in result["arms"]}
        self.assertEqual(by["JOIN_50MS"]["operational_filled_shares"], 1.0)
        self.assertEqual(by["JOIN_50MS"]["research_request"]["pessimistic_queue_ahead"], 3.0)
        self.assertEqual(by["IMPROVE1_50MS"]["operational_filled_shares"], 4.0)
        self.assertEqual(by["IMPROVE1_50MS"]["research_request"]["pessimistic_queue_ahead"], 0.0)



if __name__ == "__main__":
    unittest.main()
