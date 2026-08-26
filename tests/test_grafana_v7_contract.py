#!/usr/bin/env python3
from __future__ import annotations

import json
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
DASHBOARD = ROOT / "monitoring" / "grafana" / "dashboards" / "polymarket-v7.json"


class GrafanaV7ContractTests(unittest.TestCase):
    def test_single_v7_dashboard_covers_total_and_per_strategy_paper_state(self) -> None:
        dashboard = json.loads(DASHBOARD.read_text(encoding="utf-8"))
        self.assertEqual(dashboard["uid"], "polymarket-v7-paper")
        self.assertEqual(dashboard["title"], "Polymarket V7 PAPER")
        source = DASHBOARD.read_text(encoding="utf-8")
        for metric in (
            "polymarket_runtime_pnl_usd",
            "polymarket_runtime_equity_usd",
            "polymarket_runtime_drawdown_ratio",
            "polymarket_model_pnl_usd",
            "polymarket_model_gross_exposure_usd",
            "polymarket_model_fills_total",
            "polymarket_model_alert_staleness_seconds",
            "polymarket_v7_rank_mean_ic",
            "polymarket_v7_local_factor_by_selected_pairs",
            "polymarket_v7_hf_maker_clearable_fraction",
        ):
            self.assertIn(metric, source)
        self.assertIn("label_values(polymarket_model_info, model)", source)
        for forbidden in ("v4", "v5", "v6", "V4", "V5", "V6"):
            self.assertNotIn(forbidden, source)

    def test_monitoring_points_directly_to_v7_dashboard_and_exporter(self) -> None:
        compose = (ROOT / "docker-compose.monitoring.yml").read_text(encoding="utf-8")
        self.assertIn("/app/exporter_v7.py", compose)
        self.assertIn("polymarket-v7.json", compose)
        self.assertNotIn("exporter_latest", compose)
        self.assertNotIn("paper_v6", compose)


if __name__ == "__main__":
    unittest.main()
