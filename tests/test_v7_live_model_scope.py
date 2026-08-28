from __future__ import annotations

import json
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


class V7LiveModelScopeTest(unittest.TestCase):
    def test_scope_is_exact_twelve_family_partition(self) -> None:
        registry = json.loads((ROOT / "config/v7_strategy_registry.json").read_text())
        scope = json.loads((ROOT / "config/v7_live_model_scope.json").read_text())
        enabled = {
            row["family"] for row in registry["strategies"] if row.get("enabled") is True
        }
        target = set(scope["target_live_families"])
        excluded = set(scope["excluded_live_families"])
        self.assertEqual(len(enabled), 15)
        self.assertEqual(scope["target_live_count"], 12)
        self.assertEqual(len(target), 12)
        self.assertEqual(excluded, {"ranking", "pca", "local_factor"})
        self.assertEqual(target | excluded, enabled)
        self.assertFalse(target & excluded)
        self.assertEqual(
            set(scope["research_shadow_supervised_families"]),
            {"sports_latency", "cross_platform", "wallet_intelligence"},
        )
        self.assertTrue(scope["paper_only"])
        self.assertFalse(scope["authenticated_execution"])
        self.assertFalse(scope["real_order_submission"])

    def test_live_loop_attaches_only_the_three_additional_research_shadows(self) -> None:
        loop = (ROOT / "scripts/paper_v7_execution_loop.sh").read_text()
        self.assertIn("scripts/v7_research_shadow_supervisor.py", loop)
        self.assertIn("scripts/v7_sports_collector.py", loop)
        self.assertIn("scripts/v7_cross_platform_collector.py", loop)
        self.assertIn("scripts/v7_osint_mapping_collector.py", loop)
        self.assertIn('--scope "$LIVE_MODEL_SCOPE"', loop)
        self.assertIn('--model-sha "$SHA"', loop)
        self.assertEqual(loop.count("scripts/v7_research_shadow_supervisor.py"), 1)
        for excluded_entrypoint in (
            "v7_cross_sectional_rank.py",
            "v7_pca_stat_arb_research.py",
            "v7_local_factor_research.py",
        ):
            self.assertNotIn(excluded_entrypoint, loop)

    def test_monitoring_and_server_health_require_twelve_model_scope(self) -> None:
        manifest = json.loads((ROOT / "monitoring/v7_monitoring_manifest.json").read_text())
        supervision = manifest["supervision"]
        self.assertEqual(supervision["research_sleeves_expected"], 3)
        self.assertEqual(supervision["live_models_expected"], 12)
        self.assertEqual(supervision["live_model_scope"], "config/v7_live_model_scope.json")
        self.assertIn("control/research_sleeves_manifest.json", manifest["required_surfaces"])
        health = (ROOT / ".github/workflows/v7-paper-server-health.yml").read_text()
        for required in (
            "polymarket_v7_strategy_registry_enabled 15",
            "polymarket_v7_research_sleeves_attached 3",
            "polymarket_v7_research_supervisor_alive 1",
            "polymarket_v7_research_manifest_fresh 1",
            "polymarket_v7_live_model_target_count 12",
            "polymarket_v7_live_model_blocked_count",
            "polymarket_v7_live_model_blocked_config_count",
            "polymarket_v7_live_model_blocked_external_count",
            "polymarket_v7_live_model_scope_wired 1",
            "polymarket_v7_live_model_target_operational",
        ):
            self.assertIn(required, health)

    def test_cutover_contract_validates_scope_and_shadow_ownership(self) -> None:
        source = (ROOT / "scripts/v7_cutover_contract.py").read_text()
        for required in (
            "config/v7_live_model_scope.json",
            "target_live_count",
            "research_has_capital",
            "research_has_oms_authority",
            "research_has_ledger_writer_authority",
            "sports_latency",
            "cross_platform",
            "wallet_intelligence",
        ):
            self.assertIn(required, source)


if __name__ == "__main__":
    unittest.main()
