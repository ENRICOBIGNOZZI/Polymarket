#!/usr/bin/env python3
from __future__ import annotations

import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

from v7_capital_allocator import allocate


class CapitalAllocatorTests(unittest.TestCase):
    def test_sleeves_plus_reserve_equal_account(self) -> None:
        cfg = {"paper_only": True, "starting_capital": 10000.0, "v7": {
            "authenticated_execution": False, "real_order_submission": False,
            "relative_value_capital_fraction": .34, "hard_arb_capital_fraction": .22,
            "micro_taker_capital_fraction": .12, "micro_maker_capital_fraction": .22,
            "fast_structural_capital_fraction": 0.0,
            "external_capital_fraction": .08, "reserve_fraction": .02,
        }}
        budgets = allocate(cfg)
        self.assertAlmostEqual(sum(budgets.values()), 10000.0)
        self.assertAlmostEqual(budgets["graph_rv"], 3400.0)
        self.assertAlmostEqual(budgets["fast_structural"], 0.0)
        self.assertAlmostEqual(budgets["reserve"], 200.0)

    def test_overallocation_fails_closed(self) -> None:
        cfg = {"paper_only": True, "starting_capital": 100.0, "v7": {
            "authenticated_execution": False, "real_order_submission": False,
            "relative_value_capital_fraction": .8, "hard_arb_capital_fraction": .8,
        }}
        with self.assertRaises(ValueError):
            allocate(cfg)

    def test_authenticated_execution_is_rejected(self) -> None:
        cfg = {"paper_only": True, "starting_capital": 100.0, "v7": {"authenticated_execution": True, "real_order_submission": False}}
        with self.assertRaises(ValueError):
            allocate(cfg)


if __name__ == "__main__":
    unittest.main()
