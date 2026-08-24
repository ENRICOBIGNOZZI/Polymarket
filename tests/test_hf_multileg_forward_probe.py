#!/usr/bin/env python3
import math
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))

from forward_maker_probe import Book, Level
from hf_multileg_forward_probe import (
    CandidateLeg,
    break_even_completion,
    bundle_scale,
    l1_ofi,
    microstructure_features,
    parse_gamma_market,
    parse_leg_spec,
    select_b2_candidates,
)


class HFMultilegForwardProbeTest(unittest.TestCase):
    @staticmethod
    def book(bid_price: float, bid_size: float, ask_price: float, ask_size: float) -> Book:
        return Book(
            token_id="x",
            bids=(Level(bid_price, bid_size),),
            asks=(Level(ask_price, ask_size),),
            tick_size=0.01,
            min_order_size=1.0,
        )

    def test_parse_leg_spec(self) -> None:
        legs = parse_leg_spec("1:YES:1|2:NO:0.5|bad")
        self.assertEqual(
            [(leg.market_id, leg.outcome, leg.weight) for leg in legs],
            [("1", "YES", 1.0), ("2", "NO", 0.5)],
        )

    def test_parse_gamma_market_accepts_json_encoded_arrays(self) -> None:
        mapping = parse_gamma_market(
            {
                "id": "7",
                "conditionId": "condition",
                "outcomes": '["Yes","No"]',
                "clobTokenIds": '["token-y","token-n"]',
            }
        )
        self.assertEqual(mapping["YES"].token_id, "token-y")
        self.assertEqual(mapping["NO"].token_id, "token-n")

    def test_microprice_and_l1_imbalance(self) -> None:
        features = microstructure_features(self.book(0.40, 30.0, 0.50, 10.0))
        self.assertAlmostEqual(float(features["microprice"]), 0.475)
        self.assertAlmostEqual(float(features["imbalance_l1"]), 0.5)
        self.assertAlmostEqual(float(features["spread"]), 0.1)

    def test_snapshot_ofi_detects_bid_size_increase(self) -> None:
        previous = self.book(0.40, 10.0, 0.50, 10.0)
        current = self.book(0.40, 15.0, 0.50, 10.0)
        self.assertAlmostEqual(l1_ofi(previous, current), 5.0)

    def test_bundle_scale_respects_notional_and_leg_cap(self) -> None:
        legs = [CandidateLeg("1", "YES", 1.0), CandidateLeg("2", "NO", 2.0)]
        prices = {("1", "YES"): 0.4, ("2", "NO"): 0.3}
        self.assertAlmostEqual(bundle_scale(legs, prices, 100.0, 50.0), 25.0)

    def test_break_even_completion_probability(self) -> None:
        self.assertAlmostEqual(float(break_even_completion(0.02, -0.03)), 0.6)
        self.assertIsNone(break_even_completion(-0.01, -0.03))

    def test_candidate_selection_prefers_best_maker_economics(self) -> None:
        payload = {
            "candidates": {
                "b2": [
                    {"market": "a", "maker_entry_net_edge": "-0.01", "legs": "1:YES:1|2:NO:1"},
                    {"market": "b", "maker_entry_net_edge": "0.01", "legs": "3:YES:1|4:NO:1"},
                ]
            }
        }
        self.assertEqual(select_b2_candidates(payload, 1)[0]["market"], "b")


if __name__ == "__main__":
    unittest.main()
