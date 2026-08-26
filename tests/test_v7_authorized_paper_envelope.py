from __future__ import annotations

import json
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
CONFIG = ROOT / "config" / "paper_v7.json"


class V7AuthorizedPaperEnvelopeTest(unittest.TestCase):
    def test_authorized_paper_envelope_and_hard_safety_are_pinned(self) -> None:
        cfg = json.loads(CONFIG.read_text(encoding="utf-8"))
        self.assertTrue(cfg["paper_only"])
        self.assertEqual(cfg["market_limit"], 1000)
        self.assertEqual(cfg["min_liquidity"], 2.0)
        self.assertEqual(cfg["min_net_edge"], 0.00005)
        self.assertEqual(cfg["uncertainty_penalty"], 0.0)
        self.assertEqual(cfg["fractional_kelly"], 0.25)
        self.assertTrue(cfg["fixed_dollar_trade_cap_enabled"])
        self.assertEqual(cfg["max_trade_usd"], 125.0)
        self.assertEqual(cfg["max_market_fraction"], 0.05)
        self.assertEqual(cfg["max_event_fraction"], 0.15)
        self.assertEqual(cfg["max_gross_fraction"], 0.70)
        self.assertEqual(cfg["max_drawdown"], 0.15)
        self.assertEqual(cfg["multi_strategy"]["global_max_drawdown"], 0.15)
        self.assertEqual(cfg["multi_strategy"]["global_max_gross_fraction"], 0.70)
        self.assertTrue(cfg["v7"]["paper_only"])
        self.assertFalse(cfg["v7"]["authenticated_execution"])
        self.assertTrue(cfg["v7"]["authoritative_fee_required"])
        self.assertTrue(cfg["v7"]["shared_execution_ledger_required"])
        self.assertTrue(cfg["v7"]["joint_fill_state_required_for_multileg"])

    def test_authorized_v7_sleeves_sum_to_one(self) -> None:
        cfg = json.loads(CONFIG.read_text(encoding="utf-8"))["v7"]
        expected = {
            "micro_maker_capital_fraction": 0.22,
            "micro_taker_capital_fraction": 0.12,
            "relative_value_capital_fraction": 0.34,
            "hard_arb_capital_fraction": 0.22,
            "external_capital_fraction": 0.08,
            "reserve_fraction": 0.02,
        }
        for key, value in expected.items():
            self.assertEqual(cfg[key], value)
        self.assertAlmostEqual(sum(expected.values()), 1.0)


if __name__ == "__main__":
    unittest.main()
