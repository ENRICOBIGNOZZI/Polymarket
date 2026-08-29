#!/usr/bin/env python3
from __future__ import annotations

import importlib.util
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def load():
    spec = importlib.util.spec_from_file_location("v6_market_common_hazard_test", ROOT / "scripts/v6_market_common.py")
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


class FillHazardContracts(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.m = load()

    def test_zero_observed_flow_has_zero_fill_probability(self):
        p = self.m.fill_probability_proxy(
            queue_ahead=0, own_shares=10, compatible_flow_per_second=0,
            horizon_seconds=60, prior_flow_per_second=100,
        )
        self.assertEqual(p, 0.0)

    def test_fill_probability_decreases_with_required_queue(self):
        near = self.m.fill_probability_proxy(
            queue_ahead=5, own_shares=5, compatible_flow_per_second=1,
            horizon_seconds=30, prior_flow_per_second=0,
        )
        far = self.m.fill_probability_proxy(
            queue_ahead=100, own_shares=5, compatible_flow_per_second=1,
            horizon_seconds=30, prior_flow_per_second=0,
        )
        self.assertGreater(near, far)
        self.assertLessEqual(near, 1.0)

    def test_recent_flow_has_higher_hazard_than_stale_equal_volume(self):
        m = self.m
        recent = m.TapeFlow([m.TapeTrade(995, "t", "SELL", 0.4, 20.0)], now=1000)
        stale = m.TapeFlow([m.TapeTrade(705, "t", "SELL", 0.4, 20.0)], now=1000)
        recent_rate = recent.compatible_sell_rate("t", 0.4, lookback_seconds=300)
        stale_rate = stale.compatible_sell_rate("t", 0.4, lookback_seconds=300)
        self.assertGreater(recent_rate, stale_rate)

    def test_capacity_proxy_is_half_when_expected_flow_equals_required(self):
        p = self.m.fill_probability_proxy(
            queue_ahead=5, own_shares=5, compatible_flow_per_second=1,
            horizon_seconds=10, prior_flow_per_second=0,
        )
        self.assertAlmostEqual(p, 0.5, places=12)


if __name__ == "__main__":
    unittest.main()
