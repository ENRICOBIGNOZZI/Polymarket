#!/usr/bin/env python3
from __future__ import annotations

import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

from v7_graph_cost_vector import slippage_cost_from_effective_buy, slippage_cost_from_effective_sell_cashflow


class GraphCostVectorTests(unittest.TestCase):
    def test_buy_slippage_reconstructs_raw_execution(self) -> None:
        slip = 0.0005
        raw = 0.50
        effective = raw * (1.0 + slip)
        shares = 100.0
        self.assertAlmostEqual(slippage_cost_from_effective_buy(effective, shares, slip), shares * raw * slip, places=12)

    def test_sell_slippage_reconstructs_raw_cashflow(self) -> None:
        slip = 0.0005
        raw_cashflow = 50.0
        effective_cashflow = raw_cashflow * (1.0 - slip)
        self.assertAlmostEqual(slippage_cost_from_effective_sell_cashflow(effective_cashflow, slip), raw_cashflow * slip, places=12)

    def test_zero_slippage_is_explicit_zero(self) -> None:
        self.assertEqual(slippage_cost_from_effective_buy(0.5, 10, 0.0), 0.0)
        self.assertEqual(slippage_cost_from_effective_sell_cashflow(5.0, 0.0), 0.0)


if __name__ == "__main__":
    unittest.main()
