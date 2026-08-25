#!/usr/bin/env python3
from __future__ import annotations

import csv
import json
import subprocess
import sys
import tempfile
import time
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
        for metric in ("polymarket_model_orders_total","polymarket_model_fills_total","polymarket_model_realized_pnl_usd","polymarket_open_order_remaining_shares","polymarket_open_order_queue_ahead_shares","polymarket_recent_fill_pnl_usd","polymarket_strategy_realized_pnl_usd","polymarket_v6_queue_filter_accepted_bundles","polymarket_v6_local_factor_clusters"):
            self.assertIn(metric,exporter)

    def test_dashboard_directory_is_provisioned(self):
        provisioning=(ROOT/"monitoring"/"grafana"/"provisioning"/"dashboards"/"dashboards.yml").read_text(encoding="utf-8")
        self.assertIn("/var/lib/grafana/dashboards",provisioning)

    def test_native_status_keeps_legacy_readiness_contract(self):
        with tempfile.TemporaryDirectory() as tmp_dir:
            root=Path(tmp_dir); config=root/"paper_v6.json"
            config.write_text(json.dumps({
                "starting_capital":10000.0,"max_drawdown":0.15,"max_market_fraction":0.025,
                "max_event_fraction":0.08,"max_gross_fraction":0.45,
                "v6":{"micro_maker_capital_fraction":0.12,"micro_taker_capital_fraction":0.08,
                      "relative_value_capital_fraction":0.50,"hard_arb_capital_fraction":0.15,
                      "external_capital_fraction":0.10,"reserve_fraction":0.05},
            }),encoding="utf-8")
            subprocess.run([sys.executable,str(ROOT/"scripts/v6_runtime_status.py"),"--config",str(config),"--run-root",str(root)],check=True,capture_output=True,text=True)
            with (root/"strategy_status.csv").open(newline="",encoding="utf-8") as handle:
                rows=list(csv.DictReader(handle))
            self.assertEqual({row["name"] for row in rows},{"micro","pca","graph","semantic","external"})
            self.assertTrue(all(row["alive"]=="1" for row in rows))
            allocator=json.loads((root/"allocator_status.json").read_text(encoding="utf-8"))
            self.assertEqual(allocator["schema"],"v6_legacy_health_view"); self.assertEqual(allocator["models_alive"],5)
            now=int(time.time())
            (root/"runtime_supervisor.csv").write_text(
                "timestamp,recorder_alive,broker_alive,allocator_alive,recorder_restarts,broker_restarts,allocator_restarts,recorder_pid,broker_pid,allocator_pid\n"
                f"{now},1,1,1,0,0,0,1,2,3\n",encoding="utf-8")
            ready=subprocess.run([sys.executable,str(ROOT/"scripts/v5_runtime_readiness.py"),"--run-root",str(root)],capture_output=True,text=True)
            self.assertEqual(ready.returncode,0,ready.stdout+ready.stderr)


if __name__ == "__main__":
    unittest.main()
