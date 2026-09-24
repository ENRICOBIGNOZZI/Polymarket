from pathlib import Path
import sys
import unittest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

from v7_alpha_driven_maker_shadow import make_anchor, make_protocol


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


if __name__ == "__main__":
    unittest.main()
