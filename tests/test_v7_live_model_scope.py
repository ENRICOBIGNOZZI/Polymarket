from __future__ import annotations

import json
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
LIVE = {"CRYPTO_SETTLEMENT_ENGINE", "STRUCTURAL_ARB_ENGINE"}
REMOVED = {
    "micro_taker", "graph_rv", "ranking", "pca", "local_factor",
    "wallet_intelligence", "market_open", "osint", "sports_latency",
    "cross_platform",
}


class V7LiveModelScopeTest(unittest.TestCase):
    def test_scope_contains_only_two_live_algorithms(self) -> None:
        registry = json.loads((ROOT / "config/v7_strategy_registry.json").read_text())
        scope = json.loads((ROOT / "config/v7_live_model_scope.json").read_text())
        self.assertEqual(registry["schema"], "polymarket_v7_live_algorithm_registry_v2")
        self.assertEqual({row["id"] for row in registry["live_algorithms"]}, LIVE)
        self.assertEqual(scope["schema"], "polymarket_v7_live_engine_scope_v2")
        self.assertEqual(scope["live_algorithm_count"], 2)
        self.assertEqual(set(scope["live_algorithms"]), LIVE)
        self.assertEqual(set(scope["legacy_algorithm_families_removed"]), REMOVED)
        self.assertTrue(scope["paper_only"])
        self.assertFalse(scope["authenticated_execution"])
        self.assertFalse(scope["real_order_submission"])

    def test_removed_algorithms_are_absent_from_live_launcher(self) -> None:
        loop = (ROOT / "scripts/paper_v7_execution_loop.sh").read_text()
        for family in REMOVED:
            self.assertNotIn(family, loop)
        self.assertIn("CRYPTO_SETTLEMENT_ENGINE", loop)
        self.assertIn("STRUCTURAL_ARB_ENGINE", loop)
        self.assertIn("structural_relations", loop)

    def test_removed_algorithm_source_files_are_absent(self) -> None:
        removed_paths = (
            "scripts/v7_micro_taker_worker.py",
            "scripts/v7_graph_rv_executable_intents.py",
            "scripts/v7_research_shadow_supervisor.py",
            "scripts/v7_slow_economic_shadow_supervisor.py",
            "scripts/v7_osint_engine.py",
            "scripts/v7_sports_latency.py",
            "scripts/v7_cross_platform.py",
            "scripts/v7_wallet_intelligence.py",
            "scripts/v7_market_open.py",
            "scripts/v7_pca_stat_arb_core.py",
            "scripts/v7_local_factor_core.py",
        )
        self.assertTrue(all(not (ROOT / path).exists() for path in removed_paths))


if __name__ == "__main__":
    unittest.main()
