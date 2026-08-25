#!/usr/bin/env python3
from __future__ import annotations

import json
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
DASHBOARD = ROOT / "monitoring" / "grafana" / "dashboards" / "polymarket-v6-model-operations.json"


class GrafanaV6OperationsContractTests(unittest.TestCase):
    def test_dashboard_surfaces_orders_fills_pnl_and_fillability(self):
        dashboard=json.loads(DASHBOARD.read_text(encoding="utf-8")); self.assertEqual(dashboard["uid"],"polymarket-v6-model-operations"); titles={panel["title"] for panel in dashboard["panels"]}
        for required in ("TOTAL MARKED PNL","TOTAL REALIZED PNL","OPEN ORDERS","TOTAL FILLS","PnL by Model","Realized PnL by RV Strategy","Orders and Fills by Model","OPEN ORDERS — Remaining Shares","OPEN ORDERS — Queue Ahead","RECENT FILLS / EXECUTIONS","Execution Fillability Frontier","Local Factor Funnel","Graph RV Funnel"):
            self.assertIn(required,titles)
        expressions="\n".join(target["expr"] for panel in dashboard["panels"] for target in panel.get("targets",[]) if isinstance(target,dict) and "expr" in target)
        for metric in ("polymarket_model_pnl_usd","polymarket_model_realized_pnl_usd","polymarket_model_orders_total","polymarket_model_fills_total","polymarket_open_order_remaining_shares","polymarket_open_order_queue_ahead_shares","polymarket_recent_fill_pnl_usd","polymarket_strategy_realized_pnl_usd","polymarket_v6_queue_filter_best_joint_fill_probability","polymarket_v6_queue_filter_best_expected_fill_edge_ratio"):
            self.assertIn(metric,expressions)

    def test_v6_exporter_defines_new_operational_metrics(self):
        exporter=(ROOT/"monitoring"/"exporter_v6.py").read_text(encoding="utf-8")
        for metric in ("polymarket_model_orders_total","polymarket_model_fills_total","polymarket_model_realized_pnl_usd","polymarket_open_order_remaining_shares","polymarket_open_order_queue_ahead_shares","polymarket_recent_fill_pnl_usd","polymarket_strategy_realized_pnl_usd","polymarket_v6_queue_filter_accepted_bundles"):
            self.assertIn(metric,exporter)

    def test_dashboard_directory_is_provisioned(self):
        provisioning=(ROOT/"monitoring"/"grafana"/"provisioning"/"dashboards"/"dashboards.yml").read_text(encoding="utf-8")
        self.assertIn("/var/lib/grafana/dashboards",provisioning)


if __name__ == "__main__":
    unittest.main()
