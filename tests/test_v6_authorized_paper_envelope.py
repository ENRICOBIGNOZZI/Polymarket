#!/usr/bin/env python3
from __future__ import annotations

import json
import math
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
CONFIG = ROOT / "config" / "paper_v6.json"
LOOP = ROOT / "scripts" / "paper_v6_loop.sh"


class V6AuthorizedPaperEnvelopeTest(unittest.TestCase):
    def setUp(self) -> None:
        self.config = json.loads(CONFIG.read_text(encoding="utf-8"))
        self.v6 = self.config["v6"]
        self.multi = self.config["multi_strategy"]
        self.loop = LOOP.read_text(encoding="utf-8")

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

    def test_runtime_uses_validated_config_instead_of_historical_defaults(self) -> None:
        self.assertIn("CONFIG_MARKETS", self.loop)
        self.assertIn('MIN_LIQUIDITY="${V6_MIN_LIQUIDITY:-$CONFIG_MIN_LIQUIDITY}"', self.loop)
        self.assertIn('MARKETS="${V6_MARKETS:-$CONFIG_MARKETS}"', self.loop)
        self.assertIn('RECORDER_MARKETS="${V6_RECORDER_MARKETS:-$MARKETS}"', self.loop)
        self.assertIn('INTENT_MIN_EDGE="${V6_INTENT_MIN_EDGE:-$CONFIG_INTENT_MIN_EDGE}"', self.loop)
        self.assertIn('MAX_TRADE_USD="${V6_MAX_TRADE_USD:-$CONFIG_MAX_TRADE_USD}"', self.loop)
        self.assertIn('--max-order-usd "$MAX_TRADE_USD"', self.loop)
        self.assertIn('--min-edge "$HARD_ARB_MIN_EDGE" --max-trade-usd "$HARD_ARB_MAX_TRADE_USD"', self.loop)
        self.assertGreaterEqual(self.loop.count('--max-trade-usd "$MAX_TRADE_USD"'), 2)
        for stale in (
            'V6_MIN_LIQUIDITY:-10',
            'V6_MARKETS:-700',
            'V6_INTENT_MIN_EDGE:-0.00020',
            '--max-order-usd 60',
            '--min-edge 0.00020 --max-trade-usd 60',
        ):
            self.assertNotIn(stale, self.loop)

    def test_child_materialization_never_inflates_market_fraction(self) -> None:
        self.assertNotIn("child['max_market_fraction']=max", self.loop)
        self.assertNotIn("trade_cap/child['starting_capital']", self.loop)
        maker_cap = self.config["starting_capital"] * self.v6["micro_maker_capital_fraction"]
        effective_order_cap = min(
            self.config["max_trade_usd"],
            maker_cap * self.config["max_market_fraction"],
        )
        self.assertEqual(maker_cap, 2200.0)
        self.assertEqual(effective_order_cap, 110.0)
        self.assertLessEqual(effective_order_cap / maker_cap, 0.05)


if __name__ == "__main__":
    unittest.main()
