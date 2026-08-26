from __future__ import annotations

import inspect
import math
import unittest

from scripts import v6_micro_maker_v2 as v2
from scripts import v6_micro_maker_v4 as v4
from scripts.v6_micro_maker_v3 import (
    depth_imbalance,
    maker_toxicity_score,
    microprice_displacement,
    toxicity_adjusted_fill_probability,
)
from scripts.v6_micro_maker_v4 import persistence_gated_fill_probability


class FakeBook:
    def __init__(self, bids, asks):
        self.bids = bids
        self.asks = asks

    @property
    def bid(self):
        return self.bids[0][0]

    @property
    def ask(self):
        return self.asks[0][0]

    @property
    def spread(self):
        return self.ask - self.bid

    @property
    def mid(self):
        return 0.5 * (self.bid + self.ask)

    def depth(self, bid_side, n=5):
        levels = self.bids if bid_side else self.asks
        return sum(size for _, size in levels[:n])

    def micro(self):
        db, da = self.depth(True), self.depth(False)
        return (self.ask * db + self.bid * da) / (db + da)


class MakerToxicityTest(unittest.TestCase):
    def test_receive_time_sell_pressure_blocks_observed_toxic_pattern(self):
        score = maker_toxicity_score(
            signed_flow_short=-1.0,
            signed_flow_long=-1.0,
            micro_displacement=0.0,
            imbalance_l1=0.0,
            imbalance_l3=0.0,
            imbalance_l5=0.0,
        )
        self.assertAlmostEqual(score, 0.70)
        self.assertEqual(
            toxicity_adjusted_fill_probability(
                0.813696155,
                toxicity=score,
                hard_block_threshold=0.55,
                discount_strength=1.5,
            ),
            0.0,
        )

    def test_positive_flow_retains_profitable_touch_pattern(self):
        score = maker_toxicity_score(
            signed_flow_short=1.0,
            signed_flow_long=0.70,
            micro_displacement=0.25,
            imbalance_l1=0.20,
            imbalance_l3=0.10,
            imbalance_l5=0.10,
        )
        self.assertEqual(score, 0.0)
        raw = 0.208014985
        self.assertAlmostEqual(
            toxicity_adjusted_fill_probability(
                raw,
                toxicity=score,
                hard_block_threshold=0.55,
                discount_strength=1.5,
            ),
            raw,
        )

    def test_moderate_toxicity_discounts_without_forcing_zero_fill(self):
        score = maker_toxicity_score(
            signed_flow_short=-0.30,
            signed_flow_long=-0.20,
            micro_displacement=-0.10,
            imbalance_l1=-0.10,
            imbalance_l3=0.0,
            imbalance_l5=0.0,
        )
        raw = 0.50
        adjusted = toxicity_adjusted_fill_probability(
            raw,
            toxicity=score,
            hard_block_threshold=0.55,
            discount_strength=1.5,
        )
        self.assertGreater(adjusted, 0.0)
        self.assertLess(adjusted, raw)

    def test_low_confidence_inside_spread_still_fails_closed(self):
        self.assertEqual(
            v2.gate_inside_fill_probability(
                0.420221365,
                queue_ahead=0.0,
                confidence=0.603633041,
                min_inside_confidence=0.80,
            ),
            0.0,
        )
        self.assertAlmostEqual(
            v2.gate_inside_fill_probability(
                0.208014985,
                queue_ahead=21129.8,
                confidence=0.904186314,
                min_inside_confidence=0.80,
            ),
            0.208014985,
        )

    def test_book_features_have_correct_passive_buy_direction(self):
        bullish = FakeBook(
            bids=[(0.49, 300.0), (0.48, 200.0), (0.47, 100.0)],
            asks=[(0.51, 50.0), (0.52, 50.0), (0.53, 50.0)],
        )
        bearish = FakeBook(
            bids=[(0.49, 50.0), (0.48, 50.0), (0.47, 50.0)],
            asks=[(0.51, 300.0), (0.52, 200.0), (0.53, 100.0)],
        )
        self.assertGreater(depth_imbalance(bullish, 3), 0.0)
        self.assertLess(depth_imbalance(bearish, 3), 0.0)
        self.assertGreater(microprice_displacement(bullish), 0.0)
        self.assertLess(microprice_displacement(bearish), 0.0)
        self.assertTrue(math.isfinite(microprice_displacement(bearish)))

    def test_inside_improvement_requires_three_independent_flow_bursts(self):
        self.assertEqual(
            persistence_gated_fill_probability(
                0.42,
                queue_ahead=0.0,
                burst_count=2,
                newest_event_age_seconds=5.0,
                min_inside_bursts=3,
                max_inside_event_age_seconds=30.0,
            ),
            0.0,
        )

    def test_inside_improvement_rejects_stale_flow_even_with_three_bursts(self):
        self.assertEqual(
            persistence_gated_fill_probability(
                0.42,
                queue_ahead=0.0,
                burst_count=3,
                newest_event_age_seconds=31.0,
                min_inside_bursts=3,
                max_inside_event_age_seconds=30.0,
            ),
            0.0,
        )

    def test_inside_improvement_accepts_fresh_persistent_flow(self):
        self.assertAlmostEqual(
            persistence_gated_fill_probability(
                0.42,
                queue_ahead=0.0,
                burst_count=3,
                newest_event_age_seconds=12.0,
                min_inside_bursts=3,
                max_inside_event_age_seconds=30.0,
            ),
            0.42,
        )

    def test_persistence_gate_does_not_remove_at_touch_fill_hazard(self):
        self.assertAlmostEqual(
            persistence_gated_fill_probability(
                0.208014985,
                queue_ahead=21129.8,
                burst_count=1,
                newest_event_age_seconds=90.0,
                min_inside_bursts=3,
                max_inside_event_age_seconds=30.0,
            ),
            0.208014985,
        )

    def test_persistence_runtime_composes_directional_toxicity_and_markouts(self):
        source = inspect.getsource(v4.main)
        self.assertIn("result = v3.main()", source)
        self.assertIs(v4.base, v4.v3.base)
        self.assertIs(v4.v2, v4.v3.v2)


if __name__ == "__main__":
    unittest.main()
