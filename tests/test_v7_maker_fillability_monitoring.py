from __future__ import annotations

import importlib.util
import json
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
MONITORING = ROOT / "monitoring"
if str(MONITORING) not in sys.path:
    sys.path.insert(0, str(MONITORING))
SPEC = importlib.util.spec_from_file_location("exporter_v7_fillability", MONITORING / "exporter_v7_fillability.py")
assert SPEC and SPEC.loader
exporter = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = exporter
SPEC.loader.exec_module(exporter)


class V7MakerFillabilityMonitoringTest(unittest.TestCase):
    def test_fillability_metrics_export_full_funnel_and_reasons(self) -> None:
        report = {
            "exact_sha_ok": True,
            "root_cause": "QUEUE_COMPETITION_OR_LIFETIME",
            "simulator_bug_suspected": "NO",
            "next_experiment": "single_dimension_lifetime_or_placement_challenger",
            "funnel": {
                "orders": 34,
                "orders_effective": 34,
                "orders_rested": 34,
                "trade_reachable": 8,
                "lower_queue_depleted": 3,
                "expected_queue_depleted": 2,
                "pessimistic_queue_depleted": 1,
                "fill_opportunity_lower": 3,
                "fill_opportunity_expected": 2,
                "fill_opportunity_pessimistic": 1,
                "partial_fills": 0,
                "full_fills": 0,
                "cancelled_before_flow": 9,
                "priority_resets": 5,
            },
            "zero_fill_reasons": {"AGGRESSIVE_FLOW_REACHED_PRICE_BUT_QUEUE_NOT_DEPLETED": 8},
            "actions": [{
                "action": "JOIN", "orders": 34, "trade_reachable": 8,
                "pessimistic_queue_depleted": 1, "fill_opportunities": 1,
                "filled_orders": 0, "mean_rest_ms": 2200.0,
                "mean_near_miss_ratio": 0.4, "priority_resets": 5,
            }],
            "markets": [{
                "market_id": "m1", "orders": 10, "trade_reachable": 5,
                "fill_opportunities": 1, "filled_orders": 0, "mean_rest_ms": 2500.0,
                "mean_near_miss_ratio": 0.7, "priority_resets": 2,
            }],
        }
        lines: list[str] = []
        exporter._append_fillability_metrics(lines, report)
        payload = "\n".join(lines)
        self.assertIn("polymarket_maker_fillability_orders 34", payload)
        self.assertIn('polymarket_maker_fillability_opportunities{scenario="pessimistic"} 1', payload)
        self.assertIn('reason="AGGRESSIVE_FLOW_REACHED_PRICE_BUT_QUEUE_NOT_DEPLETED"', payload)
        self.assertIn('action="JOIN"', payload)
        self.assertIn('market="m1"', payload)
        self.assertIn('root_cause="QUEUE_COMPETITION_OR_LIFETIME"', payload)

    def test_manifest_routes_existing_canonical_port_through_fillability_wrapper(self) -> None:
        manifest = json.loads((MONITORING / "v7_monitoring_manifest.json").read_text(encoding="utf-8"))
        self.assertEqual(manifest["exporter"]["path"], "monitoring/exporter_v7_fillability.py")
        self.assertEqual(manifest["exporter"]["port"], 9108)
        self.assertTrue(manifest["paper_only"])
        self.assertFalse(manifest["authenticated_execution"])

    def test_fillability_dashboard_contains_required_diagnostics(self) -> None:
        path = MONITORING / "grafana" / "dashboards" / "polymarket-v7-maker-fillability.json"
        dashboard = json.loads(path.read_text(encoding="utf-8"))
        self.assertEqual(dashboard["uid"], "polymarket-v7-maker-fillability")
        serialized = json.dumps(dashboard)
        for metric in (
            "polymarket_maker_fillability_orders",
            "polymarket_maker_fillability_trade_reachable",
            "polymarket_maker_fillability_queue_depleted",
            "polymarket_maker_fillability_opportunities",
            "polymarket_maker_fillability_zero_fill_reason",
            "polymarket_maker_fillability_action_fill_opportunities",
            "polymarket_maker_fillability_market_near_miss_ratio",
            "polymarket_maker_fillability_priority_resets",
            "polymarket_maker_fillability_exact_ws_present",
            "polymarket_maker_fillability_exact_ws_complete",
            "polymarket_maker_fillability_exact_ws_orders",
            "polymarket_maker_fillability_exact_ws_trade_reachable",
            "polymarket_maker_fillability_exact_ws_root_cause_info",
        ):
            self.assertIn(metric, serialized)

    def test_fillability_evidence_is_collected_hourly_and_on_contract_changes(self) -> None:
        workflow = (ROOT / ".github/workflows/v7-maker-fillability-evidence.yml").read_text(
            encoding="utf-8"
        )
        self.assertIn('cron: "17 * * * *"', workflow)
        self.assertIn("github.event_name == 'schedule'", workflow)
        self.assertIn("github.event_name == 'push'", workflow)
        self.assertIn('"config/v7_professional_market_maker.json"', workflow)
        self.assertIn('"src/v7_maker_fillability_observer.cpp"', workflow)

    def test_fillability_collection_has_exact_sha_offline_fallback(self) -> None:
        workflow = (ROOT / ".github/workflows/v7-maker-fillability-evidence.yml").read_text(
            encoding="utf-8"
        )
        self.assertIn("test \"$runtime_sha\" = \"$deployed_sha\"", workflow)
        self.assertIn("if curl -fsS http://127.0.0.1:9108/maker-fillability.json", workflow)
        self.assertIn("python3 scripts/v7_maker_fillability_report.py", workflow)
        self.assertIn('--model-sha "$runtime_sha"', workflow)
        self.assertIn("tmp_report=\"$(mktemp)\"", workflow)


if __name__ == "__main__":
    unittest.main()
