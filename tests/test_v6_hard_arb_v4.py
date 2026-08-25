#!/usr/bin/env python3
from __future__ import annotations

import importlib.util
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def load():
    spec = importlib.util.spec_from_file_location("v6_hard_arb_v4_test", ROOT / "scripts/v6_hard_arb_paper_v4.py")
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


class HardArbV4Contracts(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.hard = load()

    def test_depth_aware_candidate_size(self):
        h = self.hard
        books = [
            h.DepthBook("a", [(0.39, 100.0)], [(0.40, 100.0)], 1.0, 0.01, 1, True),
            h.DepthBook("b", [(0.39, 100.0)], [(0.40, 100.0)], 1.0, 0.01, 1, True),
        ]
        fees = [h.FeeDetails(0.0, 1.0, True, True, "test") for _ in books]
        result = h.candidate_size(
            books, fees, cash_room=80.0, max_trade_usd=80.0,
            min_edge=0.001, slippage_bps=0.0,
        )
        self.assertIsNotNone(result)
        shares, edge, cost = result
        self.assertAlmostEqual(shares, 100.0, places=6)
        self.assertAlmostEqual(cost, 80.0, places=6)
        self.assertAlmostEqual(edge, 0.20, places=6)

    def test_unwind_can_be_partial_without_faking_flat(self):
        h = self.hard
        book = h.DepthBook("a", [(0.38, 3.0), (0.37, 2.0)], [(0.40, 100.0)], 1.0, 0.01, 1, True)
        fee = h.FeeDetails(0.0, 1.0, True, True, "test")
        sold, proceeds, avg, fees = h.sell_proceeds(book, 10.0, 0.0, fee)
        self.assertAlmostEqual(sold, 5.0, places=12)
        self.assertAlmostEqual(proceeds, 3 * 0.38 + 2 * 0.37, places=12)
        self.assertAlmostEqual(avg, (3 * 0.38 + 2 * 0.37) / 5.0, places=12)
        self.assertEqual(fees, 0.0)

    def test_source_persists_each_leg_and_blocks_on_unwind(self):
        text = (ROOT / "scripts/v6_hard_arb_paper_v4.py").read_text(encoding="utf-8")
        self.assertIn('aborting[event_id] = {"reason": "inflight"', text)
        self.assertIn("persist()", text)
        self.assertIn('failed_reason = "edge_revalidation"', text)
        self.assertIn('"sequential_legging_unwind_model": True', text)
        self.assertIn("if not killed and not aborting", text)
        self.assertIn("remaining_exposures", text)


if __name__ == "__main__":
    unittest.main()
