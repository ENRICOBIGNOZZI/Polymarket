#!/usr/bin/env python3
from __future__ import annotations

import json
import re
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
DASHBOARD = ROOT / "monitoring" / "grafana" / "dashboards" / "polymarket-multi-strategy.json"


class GrafanaMultiStrategyContractTests(unittest.TestCase):
    def test_dashboard_has_total_and_each_strategy_views(self) -> None:
        dashboard = json.loads(DASHBOARD.read_text(encoding="utf-8"))
        # UID is deliberately retained for in-place Grafana upgrades; changing it
        # would create a second dashboard on already-provisioned servers.
        self.assertEqual(dashboard["uid"], "polymarket-multi-strategy-v5")
        panels = dashboard["panels"]
        self.assertEqual(len({panel["id"] for panel in panels}), len(panels))
        titles = {panel["title"] for panel in panels}
        for required in (
            "TOTAL PAPER PNL",
            "PnL by Independent Strategy",
            "Equity by Independent Strategy",
            "Gross Exposure by Strategy",
            "Drawdown by Strategy",
            "Positions and Fills by Strategy",
            "Signal Funnel by Strategy",
            "Model Health and Staleness",
        ):
            self.assertIn(required, titles)

        expressions = [
            target["expr"]
            for panel in panels
            for target in panel.get("targets", [])
            if isinstance(target, dict) and "expr" in target
        ]
        joined = "\n".join(expressions)
        for metric in (
            "polymarket_runtime_pnl_usd",
            "polymarket_runtime_equity_usd",
            "polymarket_model_pnl_usd",
            "polymarket_model_equity_usd",
            "polymarket_model_drawdown_ratio",
            "polymarket_model_gross_exposure_usd",
            "polymarket_model_fills_total",
            "polymarket_model_signals_total",
            "polymarket_model_alive",
        ):
            self.assertIn(metric, joined)
        self.assertIn('polymarket_model_fills_total{model=~"$model",action="all"}', joined)

        variables = dashboard["templating"]["list"]
        self.assertEqual([item["name"] for item in variables], ["model"])
        self.assertTrue(variables[0]["includeAll"])
        self.assertTrue(variables[0]["multi"])

    def test_dashboard_queries_are_exported_by_selected_live_adapter(self) -> None:
        dashboard = json.loads(DASHBOARD.read_text(encoding="utf-8"))
        champion = json.loads((ROOT / "config" / "live_champion.json").read_text(encoding="utf-8"))
        version = int(champion["version"])
        exporter_path = ROOT / "monitoring" / f"exporter_v{version}.py"
        self.assertTrue(exporter_path.is_file(), f"missing exporter for selected live V{version}")
        exporter = exporter_path.read_text(encoding="utf-8")
        compose = (ROOT / "docker-compose.monitoring.yml").read_text(encoding="utf-8")
        self.assertIn(
            "GF_DASHBOARDS_DEFAULT_HOME_DASHBOARD_PATH: /var/lib/grafana/dashboards/polymarket-multi-strategy.json",
            compose,
        )
        expressions = "\n".join(
            target["expr"]
            for panel in dashboard["panels"]
            for target in panel.get("targets", [])
            if isinstance(target, dict) and "expr" in target
        )
        model_metrics = set(re.findall(r"\b(polymarket_model_[a-z0-9_]+)\b", expressions))
        for metric in model_metrics:
            self.assertIn(metric, exporter, f"Grafana queries {metric} but V{version} exporter does not publish it")

        # The dashboard's total-fill query is label-sensitive. This exact schema
        # mismatch previously made V6 fills appear as zero/absent even with ledger
        # fills, so keep it as a hard contract rather than a visual smoke check.
        if 'action="all"' in expressions:
            self.assertIn('"polymarket_model_fills_total"', exporter)
            self.assertIn('labels={**labels, "action": action}', exporter)
            self.assertIn('(\"all\", \"fills\")', exporter)

    def test_prometheus_alerts_cover_allocator_and_each_model(self) -> None:
        alerts = (ROOT / "monitoring" / "prometheus" / "alerts.yml").read_text(encoding="utf-8")
        for alert in (
            "PolymarketV5AllocatorDown",
            "PolymarketV5ModelProcessMissing",
            "PolymarketV5ModelStateStale",
            "PolymarketV5ModelKillSwitchActive",
            "PolymarketV5GrossLimitBreach",
        ):
            self.assertIn(f"alert: {alert}", alerts)
        self.assertIn(
            "polymarket_allocator_models_alive < polymarket_allocator_models_expected",
            alerts,
        )
        self.assertIn("max(polymarket_model_alert_staleness_seconds) > 60", alerts)
        self.assertNotIn("max(polymarket_model_status_age_seconds) > 60", alerts)
        self.assertIn("max(polymarket_model_kill_switch) == 1", alerts)
        self.assertIn(
            "polymarket_allocator_global_gross_fraction > polymarket_allocator_global_max_gross_fraction",
            alerts,
        )


if __name__ == "__main__":
    unittest.main()
