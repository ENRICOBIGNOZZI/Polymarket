from pathlib import Path
import sys
import unittest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

from v7_alpha_driven_maker_shadow import (
    POLICIES,
    RotatingBookTimeline,
    active_markets,
    make_anchor,
    make_protocol,
    pm_features,
    policy_flags,
)


class AlphaDrivenMakerShadowTests(unittest.TestCase):
    def test_protocol_grid_is_bounded_and_deterministic(self):
        p = make_protocol([1000, 250, 500, 250], [5000, 250, 1000])
        self.assertEqual(
            [a["id"] for a in p["maker"]["arms"]],
            [
                "JOIN_250MS", "JOIN_500MS", "JOIN_1000MS",
                "IMPROVE1_250MS", "IMPROVE1_500MS", "IMPROVE1_1000MS",
            ],
        )
        self.assertEqual(p["maker"]["markout_horizons_ms"], [250, 1000, 5000])

    def test_selection_keeps_only_active_m5_m15(self):
        sha = "a" * 40
        value = {
            "schema": "polymarket_v7_multi_crypto_book_selection_v1",
            "model_sha": sha,
            "paper_only": True,
            "authenticated_execution": False,
            "real_order_submission": False,
            "execution_authority": False,
            "selection_only": True,
            "markets": [
                {
                    "asset": "BTC", "horizon": "M5", "market_id": "active",
                    "yes_token": "y1", "no_token": "n1",
                    "start_timestamp_ms": 1000, "end_timestamp_ms": 2000,
                },
                {
                    "asset": "ETH", "horizon": "M15", "market_id": "future",
                    "yes_token": "y2", "no_token": "n2",
                    "start_timestamp_ms": 2000, "end_timestamp_ms": 3000,
                },
                {
                    "asset": "SOL", "horizon": "H1", "market_id": "wrong-horizon",
                    "yes_token": "y3", "no_token": "n3",
                    "start_timestamp_ms": 1000, "end_timestamp_ms": 2000,
                },
            ],
        }
        rows = active_markets(value, sha, 1500)
        self.assertEqual([r["market_id"] for r in rows], ["active"])

    def test_alpha_policy_contract(self):
        external = {"ready": True, "direction": 1, "confidence": 0.8}
        row = {
            "placement_features": {
                "imbalance": 0.4,
                "ofi": 0.2,
                "short_return_ticks": 1.0,
                "aggressive_buy_prints_per_second": 4.0,
                "aggressive_sell_prints_per_second": 1.0,
            }
        }
        pm = pm_features(row)
        yes = policy_flags(outcome="YES", external=external, pm=pm)
        no = policy_flags(outcome="NO", external=external, pm=pm)
        self.assertEqual(tuple(yes), POLICIES)
        self.assertTrue(yes["A0_BASELINE"])
        self.assertTrue(yes["A1_EXTERNAL_MOMENTUM"])
        self.assertTrue(yes["A2_PM_MOMENTUM"])
        self.assertTrue(yes["A3_COMBINED_MOMENTUM"])
        self.assertFalse(no["A1_EXTERNAL_MOMENTUM"])
        self.assertFalse(no["A5_TOXICITY_VETO"])

    def test_mean_reversion_requires_rebound_support(self):
        external = {"ready": True, "direction": 1, "confidence": 0.8}
        row = {
            "placement_features": {
                "imbalance": 0.3,
                "ofi": 0.1,
                "short_return_ticks": -2.0,
                "aggressive_buy_prints_per_second": 3.0,
                "aggressive_sell_prints_per_second": 1.0,
            }
        }
        flags = policy_flags(outcome="YES", external=external, pm=pm_features(row))
        self.assertTrue(flags["A4_MEAN_REVERSION"])

    def test_rotating_reader_uses_nonempty_segment_when_current_is_empty(self):
        import tempfile
        import time
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            (root / "current.jsonl").write_bytes(b"")
            segment = root / "session.segment-1000000.jsonl"
            segment.write_text("{}\n", encoding="utf-8")
            now = time.time_ns()
            segment.touch()
            reader = RotatingBookTimeline(root, "a" * 40, retention_ms=10_000)
            self.assertEqual(reader._candidate(), segment)

    def test_anchor_is_zero_authority_counterfactual(self):
        row = {
            "best_bid": 0.49,
            "receive_wall_ms": 123456,
            "observer_sequence": 77,
            "receive_monotonic_ns": 999_000_000,
            "exchange_event_ns": 888_000_000,
        }
        market = {
            "market_id": "m1", "asset": "BTC", "horizon": "M5",
            "yes_token": "t1", "no_token": "t2",
        }
        anchor = make_anchor(
            row, market=market, token_id="t1", outcome="YES",
            model_sha="a" * 40, quantity=5.0,
            external={"ready": False}, pm={"ready": False},
            policies={name: name == "A0_BASELINE" for name in POLICIES},
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
