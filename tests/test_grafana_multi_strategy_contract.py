#!/usr/bin/env python3
from __future__ import annotations

import json
import re
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
DASHBOARD = ROOT / "monitoring" / "grafana" / "dashboards" / "polymarket-multi-strategy.json"


class GrafanaMultiStrategyContractTests(unittest.TestCase):
    def test_dashboard_is_v7_only_and_has_total_and_strategy_views(self) -> None:
        dashboard = json.loads(DASHBOARD.read_text(encoding="utf-8"))
        self.assertEqual(dashboard["uid"], "polymarket-multi-strategy-v7")
        self.assertEqual(dashboard["title"], "Polymarket V7 Multi-Strategy")
        self.assertIn("v7", {str(tag).lower() for tag in dashboard.get("tags", [])})
        panels = dashboard["panels"]
        self.assertEqual(len({panel["id"] for panel in panels}), len(panels))
        titles = {panel["title"] for panel in panels}
        for required in (
            "TOTAL PAPER PNL",
            "PnL by V7 Strategy",
            "Equity by V7 Strategy",
            "Gross Exposure by V7 Strategy",
            "Drawdown by V7 Strategy",
            "Positions and Fills by V7 Strategy",
            "V7 Strategy Health",
            "V7 PCA Shadow Selection",
            "V7 Local Factor Signals",
            "V7 Cross-sectional Ranking",
            "V7 HF Queue Clearability",
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
            "polymarket_model_open_positions",
            "polymarket_model_alive",
            "polymarket_model_kill_switch",
            "polymarket_model_staleness_seconds",
            "polymarket_v7_pca_bh_survivors",
            "polymarket_v7_local_factor_signals",
            "polymarket_v7_rank_mean_ic",
            "polymarket_v7_hf_maker_clearable_fraction",
        ):
            self.assertIn(metric, joined)
        self.assertNotIn('action="all"', joined)
        self.assertNotIn("polymarket_model_signals_total", joined)
        self.assertNotIn("polymarket_model_execution_evidence_", joined)

        variables = dashboard["templating"]["list"]
        self.assertEqual([item["name"] for item in variables], ["model"])
        self.assertTrue(variables[0]["includeAll"])
        self.assertTrue(variables[0]["multi"])

    def test_every_dashboard_metric_is_published_by_v7_exporter(self) -> None:
        dashboard = json.loads(DASHBOARD.read_text(encoding="utf-8"))
        champion = json.loads((ROOT / "config" / "live_champion.json").read_text(encoding="utf-8"))
        self.assertEqual(champion["version"], 7)
        exporter = (ROOT / "monitoring" / "exporter_v7.py").read_text(encoding="utf-8")
        compose = (ROOT / "docker-compose.monitoring.yml").read_text(encoding="utf-8")
        self.assertIn("/app/exporter_latest_v7.py", compose)
        self.assertIn("GF_DASHBOARDS_DEFAULT_HOME_DASHBOARD_PATH: /var/lib/grafana/dashboards/polymarket-multi-strategy.json", compose)
        expressions = "\n".join(
            target["expr"]
            for panel in dashboard["panels"]
            for target in panel.get("targets", [])
            if isinstance(target, dict) and "expr" in target
        )
        metrics = set(re.findall(r"\b(polymarket_[a-z0-9_]+)\b", expressions))
        for metric in metrics:
            self.assertIn(metric, exporter, f"Grafana queries {metric} but V7 exporter does not publish it")

    def test_prometheus_alerts_are_v7_only(self) -> None:
        alerts = (ROOT / "monitoring" / "prometheus" / "alerts.yml").read_text(encoding="utf-8")
        for alert in (
            "PolymarketV7ExporterDown",
            "PolymarketV7RuntimeDrawdownWarning",
            "PolymarketV7RuntimeDrawdownLimit",
            "PolymarketV7KillSwitchActive",
            "PolymarketV7AllocatorMissing",
            "PolymarketV7StrategyMissing",
            "PolymarketV7StrategyStateStale",
            "PolymarketV7StrategyKillSwitchActive",
            "PolymarketV7GrossLimitBreach",
        ):
            self.assertIn(f"alert: {alert}", alerts)
        self.assertIn("polymarket_allocator_models_alive < polymarket_allocator_models_expected", alerts)
        self.assertIn("max(polymarket_model_staleness_seconds) > 120", alerts)
        self.assertIn("max(polymarket_model_kill_switch) == 1", alerts)
        for token in ("PolymarketV5", "PolymarketV6", "simulated-live", "legacy"):
            self.assertNotIn(token, alerts)


if __name__ == "__main__":
    unittest.main()
