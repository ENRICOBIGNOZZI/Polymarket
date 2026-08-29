#!/usr/bin/env python3
from __future__ import annotations

import json
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

from v7_capital_allocator import (
    EXECUTION_STRATEGIES,
    allocate,
    materialize,
    strategy_budgets_from_sleeves,
)


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

    def test_current_paper_config_assigns_exactly_2k_to_seven_execution_strategies(self) -> None:
        cfg = json.loads((ROOT / "config/paper_v7.json").read_text())
        budgets = allocate(cfg)
        target = float(cfg["v7"]["execution_strategy_budget_usd"])
        strategy_budgets = strategy_budgets_from_sleeves(
            budgets, target_budget=target
        )
        self.assertEqual(float(cfg["starting_capital"]), 14000.0)
        self.assertEqual(set(strategy_budgets), EXECUTION_STRATEGIES)
        self.assertEqual(len(strategy_budgets), 7)
        self.assertTrue(
            all(abs(budget - 2000.0) <= 1e-9 for budget in strategy_budgets.values())
        )
        self.assertAlmostEqual(sum(strategy_budgets.values()), 14000.0)
        self.assertAlmostEqual(budgets["external"], 4000.0)
        self.assertAlmostEqual(budgets["reserve"], 0.0)
        self.assertTrue(cfg["paper_only"])
        self.assertFalse(cfg["v7"]["authenticated_execution"])
        self.assertFalse(cfg["v7"]["real_order_submission"])

    def test_materialized_manifest_and_children_are_strategy_auditable(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            manifest = materialize(
                ROOT / "config/paper_v7.json", Path(tmp) / "allocations"
            )
            self.assertEqual(
                manifest["schema"], "polymarket_v7_capital_allocation_v2"
            )
            self.assertEqual(manifest["execution_strategy_count"], 7)
            self.assertAlmostEqual(manifest["strategy_budget_sum"], 14000.0)
            self.assertEqual(manifest["research_strategy_budgets"], {})
            self.assertFalse(manifest["research_has_capital"])
            self.assertFalse(manifest["real_capital_at_risk"])
            external = json.loads(
                (Path(tmp) / "allocations" / "external.json").read_text()
            )
            self.assertEqual(
                external["capital_scope"]["strategy_budgets"],
                {
                    "crypto_informed_taker": 2000.0,
                    "crypto_settlement_fair": 2000.0,
                },
            )
            self.assertAlmostEqual(
                external["capital_scope"]["strategy_budget_sum"], 4000.0
            )

    def test_configured_target_mismatch_fails_closed(self) -> None:
        budgets = {
            "fast_structural": 100.0,
            "graph_rv": 100.0,
            "hard_arb": 100.0,
            "micro_taker": 100.0,
            "micro_maker": 100.0,
            "external": 100.0,
        }
        with self.assertRaises(ValueError):
            strategy_budgets_from_sleeves(budgets, target_budget=100.0)

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
