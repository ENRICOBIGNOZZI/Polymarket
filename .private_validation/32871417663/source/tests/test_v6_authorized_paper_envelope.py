#!/usr/bin/env python3
from __future__ import annotations

import json
import math
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
CONFIG = ROOT / "config" / "paper_v6.json"


class V6AuthorizedPaperEnvelopeTest(unittest.TestCase):
    def setUp(self) -> None:
        self.config = json.loads(CONFIG.read_text(encoding="utf-8"))
        self.v6 = self.config["v6"]
        self.multi = self.config["multi_strategy"]

    def test_authorized_aggressive_envelope_is_active(self) -> None:
        self.assertEqual(self.config["market_limit"], 1000)
        self.assertEqual(self.config["min_liquidity"], 2.0)
        self.assertEqual(self.config["min_net_edge"], 0.00005)
        self.assertEqual(self.config["uncertainty_penalty"], 0.0)
        self.assertEqual(self.config["fractional_kelly"], 0.25)
        self.assertEqual(self.config["max_trade_usd"], 125.0)
        self.assertEqual(self.config["max_market_fraction"], 0.05)
        self.assertEqual(self.config["max_event_fraction"], 0.15)
        self.assertEqual(self.config["max_gross_fraction"], 0.70)
        self.assertEqual(self.config["max_drawdown"], 0.15)

    def test_v6_sleeve_allocation_matches_authorized_envelope(self) -> None:
        expected = {
            "micro_maker_capital_fraction": 0.22,
            "micro_taker_capital_fraction": 0.12,
            "relative_value_capital_fraction": 0.34,
            "hard_arb_capital_fraction": 0.22,
            "external_capital_fraction": 0.08,
            "reserve_fraction": 0.02,
        }
        for key, value in expected.items():
            self.assertEqual(self.v6[key], value)
        self.assertTrue(math.isclose(sum(expected.values()), 1.0, rel_tol=0.0, abs_tol=1e-12))

    def test_execution_and_hard_safety_bounds_remain_fail_closed(self) -> None:
        self.assertIs(self.v6["paper_only"], True)
        self.assertIs(self.multi["paper_only"], True)
        self.assertEqual(self.multi["global_max_drawdown"], 0.15)
        self.assertEqual(self.multi["global_max_gross_fraction"], 0.70)
        self.assertEqual(self.v6["intent_min_edge"], 0.00005)
        self.assertEqual(self.v6["hard_arb_min_net_edge"], 0.00005)
        self.assertEqual(self.v6["hard_arb_max_trade_usd"], 125.0)


if __name__ == "__main__":
    unittest.main()
