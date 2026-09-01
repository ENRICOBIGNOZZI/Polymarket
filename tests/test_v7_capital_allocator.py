#!/usr/bin/env python3
from __future__ import annotations

import json
import sys
import tempfile
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

from v7_capital_allocator import (  # noqa: E402
    ALLOCATOR_OWNER, ENGINES, allocate, component_observation_budgets, materialize,
)


class CapitalAllocatorTests(unittest.TestCase):
    def config(self, btc: float, structural: float, reserve: float) -> dict:
        return {
            "paper_only": True,
            "starting_capital": 10_000.0,
            "v7": {
                "authenticated_execution": False,
                "real_order_submission": False,
                "capital_authority_owner": ALLOCATOR_OWNER,
                "engine_capital_fractions": {
                    "CRYPTO_SETTLEMENT_ENGINE": btc,
                    "STRUCTURAL_ARB_ENGINE": structural,
                },
                "component_observation_budget_fractions": {
                    "professional_maker": 0.2,
                    "crypto_informed_taker": 0.0,
                    "fast_structural": 0.1,
                },
                "reserve_fraction": reserve,
            },
        }

    def test_engine_envelopes_plus_reserve_equal_account(self) -> None:
        budgets = allocate(self.config(0.4, 0.2, 0.1))
        self.assertAlmostEqual(sum(budgets.values()), 10_000.0)
        self.assertEqual(budgets["CRYPTO_SETTLEMENT_ENGINE"], 4_000.0)
        self.assertEqual(budgets["STRUCTURAL_ARB_ENGINE"], 2_000.0)
        self.assertAlmostEqual(budgets["reserve"], 4_000.0)

    def test_current_config_has_two_engine_envelopes_and_one_owner(self) -> None:
        cfg = json.loads((ROOT / "config/paper_v7.json").read_text())
        budgets = allocate(cfg)
        self.assertEqual(set(budgets), {*ENGINES, "reserve"})
        self.assertEqual(budgets["CRYPTO_SETTLEMENT_ENGINE"], 4_000.0)
        self.assertEqual(budgets["STRUCTURAL_ARB_ENGINE"], 2_000.0)
        self.assertAlmostEqual(budgets["reserve"], 8_000.0)
        self.assertEqual(cfg["v7"]["capital_authority_owner"], ALLOCATOR_OWNER)
        self.assertNotIn("capital_authority_owners", cfg["v7"])
        self.assertFalse(any(key.endswith("_capital_fraction") for key in cfg["v7"]))

    def test_component_observation_budget_is_not_capital(self) -> None:
        cfg = json.loads((ROOT / "config/paper_v7.json").read_text())
        self.assertEqual(component_observation_budgets(cfg), {
            "crypto_informed_taker": 0.0,
            "fast_structural": 2_000.0,
            "professional_maker": 2_000.0,
        })
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "allocations"
            manifest = materialize(ROOT / "config/paper_v7.json", root)
            self.assertEqual(manifest["schema"], "polymarket_v7_capital_allocation_v3")
            self.assertEqual(manifest["capital_authority_owner_count"], 1)
            self.assertEqual(manifest["engine_count"], 2)
            self.assertEqual(manifest["engine_budget_sum"], 6_000.0)
            self.assertAlmostEqual(manifest["reserve_budget"], 8_000.0)
            self.assertFalse(manifest["component_observation_budgets_are_capital"])
            self.assertNotIn("research_budgets", manifest)
            maker = json.loads((root / "micro_maker.json").read_text())
            self.assertEqual(maker["starting_capital"], 0.0)
            self.assertEqual(maker["capital_scope"]["observation_budget"], 2_000.0)
            self.assertFalse(maker["capital_scope"]["observation_budget_is_capital"])
            fast = json.loads((root / "fast_structural.json").read_text())
            self.assertEqual(fast["starting_capital"], 0.0)
            self.assertEqual(fast["capital_scope"]["execution_budget"], 0.0)
            self.assertEqual(fast["capital_scope"]["observation_budget"], 2_000.0)
            self.assertEqual(fast["capital_scope"]["scope_class"], "COMPONENT_OBSERVATION")
            self.assertFalse(fast["capital_scope"]["observation_budget_is_capital"])
            self.assertFalse(fast["capital_scope"]["independent_capital_authority"])
            self.assertFalse(fast["capital_scope"]["independent_oms_authority"])
            self.assertFalse(fast["capital_scope"]["independent_ledger_authority"])
            self.assertTrue(fast["paper_only"])
            self.assertEqual(fast["execution_mode"], "PAPER_SIMULATED")
            self.assertTrue(fast["v7"]["paper_only"])
            self.assertEqual(fast["v7"]["execution_mode"], "PAPER_SIMULATED")
            self.assertFalse(fast["v7"]["authenticated_execution"])
            self.assertFalse(fast["v7"]["real_order_submission"])
            self.assertFalse(fast["v7"]["live_capability"]["live_enabled"])
            self.assertEqual(fast["v7"]["live_capability"]["max_daily_loss"], 0.0)
            self.assertEqual(fast["v7"]["live_capability"]["max_exposure"], 0.0)
            self.assertEqual(fast["v7"]["live_capability"]["max_order"], 0.0)
            external = json.loads((root / "external.json").read_text())
            self.assertEqual(external["starting_capital"], 4_000.0)
            self.assertEqual(external["capital_scope"]["engine_id"], "CRYPTO_SETTLEMENT_ENGINE")
            self.assertEqual(external["capital_scope"]["scope_class"], "TEMPORARY_ENGINE_ADAPTER")

    def test_duplicate_or_unknown_engine_partition_fails_closed(self) -> None:
        cfg = self.config(0.4, 0.2, 0.1)
        cfg["v7"]["engine_capital_fractions"]["THIRD_ENGINE"] = 0.1
        with self.assertRaisesRegex(ValueError, "engine_capital_fraction_partition"):
            allocate(cfg)

    def test_overallocation_and_wrong_owner_fail_closed(self) -> None:
        with self.assertRaisesRegex(ValueError, "capital_fractions_exceed_one"):
            allocate(self.config(0.8, 0.8, 0.0))
        cfg = self.config(0.4, 0.2, 0.1)
        cfg["v7"]["capital_authority_owner"] = "SECOND_ALLOCATOR"
        with self.assertRaisesRegex(ValueError, "canonical_owner"):
            allocate(cfg)


if __name__ == "__main__":
    unittest.main()
