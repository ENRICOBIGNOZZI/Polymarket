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
    @staticmethod
    def _write_runtime_inputs(root: Path, now: int, *, micro_timestamp: int | None = None) -> None:
        (root / "maker").mkdir(parents=True, exist_ok=True)
        (root / "micro_taker").mkdir(parents=True, exist_ok=True)
        (root / "hard_arb").mkdir(parents=True, exist_ok=True)
        (root / "external").mkdir(parents=True, exist_ok=True)
        (root / "maker" / "maker_equity.csv").write_text(
            "timestamp,cash,equity,reserved_cash,resting_orders,positions,peak_equity,drawdown,killed\n"
            f"{now},1200,1200,0,0,0,1200,0,0\n",
            encoding="utf-8",
        )
        (root / "multileg_equity.csv").write_text(
            "timestamp,cash,equity,reserved_cash,gross_entry_cash,peak_equity,drawdown,killed,live_bundles\n"
            f"{now},5000,5000,0,0,5000,0,0,0\n",
            encoding="utf-8",
        )
        for path, payload in (
            (root / "micro_taker" / "status.json", {"timestamp": micro_timestamp or now, "cash": 800, "equity": 800}),
            (root / "hard_arb" / "status.json", {"timestamp": now, "cash": 1500, "equity": 1500}),
            (root / "external" / "status.json", {"timestamp": now, "cash": 1000, "equity": 1000}),
            (root / "relation_status.json", {"timestamp": now}),
            (root / "graph_research_status.json", {"timestamp": now}),
            (root / "local_factor_status.json", {"timestamp": now}),
            (root / "external_bridge_status.json", {"timestamp": now}),
            (root / "market_proxy_status.json", {"timestamp": now, "source": "gamma_keyset", "markets": 100, "cache_age_seconds": 0}),
        ):
            path.write_text(json.dumps(payload), encoding="utf-8")
        (root / "trade_recorder_state.csv").write_text(
            f"last_trade_ts,seen_count\n{now},1\n", encoding="utf-8"
        )
        (root / "runtime_supervisor.csv").write_text(
            "timestamp,recorder_alive,broker_alive,allocator_alive,recorder_restarts,broker_restarts,allocator_restarts,recorder_pid,broker_pid,allocator_pid\n"
            f"{now},1,1,1,0,0,0,1,2,3\n",
            encoding="utf-8",
        )

    def test_dashboard_surfaces_orders_fills_pnl_and_fillability(self):
        dashboard=json.loads(DASHBOARD.read_text(encoding="utf-8")); self.assertEqual(dashboard["uid"],"polymarket-v6-model-operations"); titles={panel["title"] for panel in dashboard["panels"]}
        for required in ("TOTAL MARKED PNL","TOTAL REALIZED PNL","OPEN ORDERS","TOTAL FILLS","PnL by Model","Realized PnL by RV Strategy","Orders and Fills by Model","OPEN ORDERS — Remaining Shares","OPEN ORDERS — Queue Ahead","RECENT FILLS / EXECUTIONS","Execution Fillability Frontier","Local Factor Funnel","Graph RV — Research Only","Taker Exploration — Paper Only"):
            self.assertIn(required,titles)
        expressions="\n".join(target["expr"] for panel in dashboard["panels"] for target in panel.get("targets",[]) if isinstance(target,dict) and "expr" in target)
        for metric in ("polymarket_model_pnl_usd","polymarket_model_realized_pnl_usd","polymarket_model_orders_total","polymarket_model_fills_total","polymarket_open_order_remaining_shares","polymarket_open_order_queue_ahead_shares","polymarket_recent_fill_pnl_usd","polymarket_strategy_realized_pnl_usd","polymarket_v6_queue_filter_best_joint_fill_probability","polymarket_v6_queue_filter_best_expected_fill_edge_ratio","polymarket_v6_graph_research_candidate_bundles","polymarket_v6_graph_research_broker_routing_enabled","polymarket_v6_micro_taker_exploration_active_positions"):
            self.assertIn(metric,expressions)

    def test_v6_exporter_defines_new_operational_metrics(self):
        exporter=(ROOT/"monitoring"/"exporter_v6.py").read_text(encoding="utf-8")
        for metric in ("polymarket_model_orders_total","polymarket_model_fills_total","polymarket_model_realized_pnl_usd","polymarket_open_order_remaining_shares","polymarket_open_order_queue_ahead_shares","polymarket_recent_fill_pnl_usd","polymarket_strategy_realized_pnl_usd","polymarket_v6_queue_filter_accepted_bundles","polymarket_v6_local_factor_clusters","polymarket_v6_graph_research_candidate_bundles","polymarket_v6_graph_research_broker_routing_enabled","polymarket_v6_micro_taker_exploration_active_positions"):
            self.assertIn(metric,exporter)

    def test_dashboard_directory_is_provisioned(self):
        provisioning=(ROOT/"monitoring"/"grafana"/"provisioning"/"dashboards"/"dashboards.yml").read_text(encoding="utf-8")
        self.assertIn("/var/lib/grafana/dashboards",provisioning)

    def test_native_status_fails_closed_without_sleeve_outputs(self):
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
            self.assertTrue(all(row["alive"]=="0" for row in rows))
            self.assertTrue(all(float(row["status_age_seconds"]) >= 1e12 for row in rows))
            allocator=json.loads((root/"allocator_status.json").read_text(encoding="utf-8"))
            self.assertEqual(allocator["schema"],"v6_legacy_health_view"); self.assertEqual(allocator["models_alive"],0)

    def test_native_status_keeps_legacy_readiness_contract_with_fresh_inputs(self):
        with tempfile.TemporaryDirectory() as tmp_dir:
            root=Path(tmp_dir); config=root/"paper_v6.json"
            config.write_text(json.dumps({
                "starting_capital":10000.0,"max_drawdown":0.15,"max_market_fraction":0.025,
                "max_event_fraction":0.08,"max_gross_fraction":0.45,
                "v6":{"micro_maker_capital_fraction":0.12,"micro_taker_capital_fraction":0.08,
                      "relative_value_capital_fraction":0.50,"hard_arb_capital_fraction":0.15,
                      "external_capital_fraction":0.10,"reserve_fraction":0.05},
            }),encoding="utf-8")
            now=int(time.time())
            self._write_runtime_inputs(root, now)
            commands=[
                [sys.executable,str(ROOT/"scripts/v6_runtime_status.py"),"--config",str(config),"--run-root",str(root)]
                for _ in range(8)
            ]
            processes=[subprocess.Popen(command,stdout=subprocess.PIPE,stderr=subprocess.PIPE,text=True) for command in commands]
            for process in processes:
                stdout,stderr=process.communicate(timeout=10)
                self.assertEqual(process.returncode,0,stdout+stderr)
            allocator=json.loads((root/"allocator_status.json").read_text(encoding="utf-8"))
            self.assertEqual(allocator["models_alive"],5)
            runtime=json.loads((root/"runtime_status.json").read_text(encoding="utf-8"))
            self.assertLess(runtime["execution_staleness"],10)
            ready=subprocess.run([sys.executable,str(ROOT/"scripts/v5_runtime_readiness.py"),"--run-root",str(root)],capture_output=True,text=True)
            self.assertEqual(ready.returncode,0,ready.stdout+ready.stderr)

    def test_native_status_reports_stale_sleeve_and_fill_imbalance(self):
        with tempfile.TemporaryDirectory() as tmp_dir:
            root=Path(tmp_dir); config=root/"paper_v6.json"; now=int(time.time())
            config.write_text((ROOT/"config/paper_v6.json").read_text(encoding="utf-8"),encoding="utf-8")
            self._write_runtime_inputs(root,now,micro_timestamp=now-300)
            (root/"multileg_legs.csv").write_text(
                "bundle_id,target_shares,filled_shares\nb1,10,10\nb1,10,2\nb2,10,10\nb2,10,1\n",encoding="utf-8"
            )
            (root/"multileg_bundles.csv").write_text(
                "bundle_id,status\nb1,RESTING\nb2,UNWOUND\n",encoding="utf-8"
            )
            subprocess.run([sys.executable,str(ROOT/"scripts/v6_runtime_status.py"),"--config",str(config),"--run-root",str(root)],check=True,capture_output=True,text=True)
            runtime=json.loads((root/"runtime_status.json").read_text(encoding="utf-8"))
            allocator=json.loads((root/"allocator_status.json").read_text(encoding="utf-8"))
            self.assertGreaterEqual(runtime["execution_staleness"],299)
            self.assertAlmostEqual(runtime["execution_imbalance"],0.8)
            self.assertEqual(allocator["models_alive"],4)

    def test_native_status_rejects_fresh_but_invalid_runtime_outputs(self):
        with tempfile.TemporaryDirectory() as tmp_dir:
            root=Path(tmp_dir); config=root/"paper_v6.json"; now=int(time.time())
            config.write_text((ROOT/"config/paper_v6.json").read_text(encoding="utf-8"),encoding="utf-8")
            self._write_runtime_inputs(root,now)
            (root/"micro_taker"/"status.json").write_text("{}",encoding="utf-8")
            (root/"market_proxy_status.json").write_text(json.dumps({
                "timestamp":now,"source":"unavailable","markets":0,"cache_age_seconds":1e12,
            }),encoding="utf-8")
            (root/"runtime_supervisor.csv").write_text(
                "timestamp,recorder_alive,broker_alive,allocator_alive\n"
                f"{now},0,0,0\n",encoding="utf-8"
            )
            subprocess.run([sys.executable,str(ROOT/"scripts/v6_runtime_status.py"),"--config",str(config),"--run-root",str(root)],check=True,capture_output=True,text=True)
            runtime=json.loads((root/"runtime_status.json").read_text(encoding="utf-8"))
            allocator=json.loads((root/"allocator_status.json").read_text(encoding="utf-8"))
            self.assertGreaterEqual(runtime["execution_staleness"],1e12)
            self.assertLess(allocator["models_alive"],5)
            self.assertFalse(runtime["proxy_ready"])
            self.assertFalse(runtime["supervisor_ready"])
            self.assertIn("market_proxy",runtime["unready_components"])
            self.assertIn("supervisor",runtime["unready_components"])
            self.assertIn("micro_taker",runtime["unready_components"])


if __name__ == "__main__":
    unittest.main()
