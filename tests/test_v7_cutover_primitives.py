from __future__ import annotations

import csv
import importlib.util
import json
import tempfile
import time
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def load_module(name: str, path: Path):
    spec = importlib.util.spec_from_file_location(name, path)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class V7CutoverPrimitiveTests(unittest.TestCase):
    def test_workflows_are_v7_exact_sha_and_fail_closed(self) -> None:
        deploy = (ROOT / ".github/workflows/deploy-paper-server.yml").read_text(encoding="utf-8")
        health = (ROOT / ".github/workflows/server-health.yml").read_text(encoding="utf-8")
        for text in (deploy, health):
            self.assertIn("paper-validated", text)
            self.assertIn("manifest.get('version') != 7", text)
            self.assertIn("authenticated_execution", text)
            self.assertNotIn("v4-live-paper-smoke", text)
            self.assertNotIn("paper_v6_loop.sh", text)
        self.assertIn("POLYMARKET_EXPECTED_SHA", deploy)
        self.assertIn("git merge-base --is-ancestor", deploy)
        self.assertIn("--require-monitoring", deploy)
        self.assertIn("health_result=no_operational_champion", health)

    def test_updater_has_no_deleted_runtime_dependency(self) -> None:
        text = (ROOT / "ops/update_server.sh").read_text(encoding="utf-8")
        self.assertIn("POLYMARKET_EXPECTED_SHA", text)
        self.assertIn("m.get('version') != 7", text)
        self.assertIn("assert_no_legacy_writer", text)
        self.assertIn("failed_full_health", text)
        for forbidden in (
            "tests/test_monitoring_exporter.py",
            "monitoring/exporter_v6.py",
            "monitoring/exporter.py monitoring/exporter_latest.py",
            "config/paper_v6.json",
            "docker-compose.paper-v6",
        ):
            self.assertNotIn(forbidden, text)

    def test_exporter_rejects_disabled_champion(self) -> None:
        mod = load_module("v7_exporter_disabled", ROOT / "monitoring/v7_exporter.py")
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "config").mkdir()
            (root / "config/live_champion.json").write_text(json.dumps({
                "enabled": False, "version": None, "paper_only": True, "authenticated_execution": False
            }), encoding="utf-8")
            collector = mod.V7Collector(root, Path("config/live_champion.json"), 180)
            with self.assertRaisesRegex(RuntimeError, "disabled"):
                collector.snapshot()

    def test_exporter_emits_economic_and_strategy_metrics(self) -> None:
        mod = load_module("v7_exporter_fixture", ROOT / "monitoring/v7_exporter.py")
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "config").mkdir()
            run_root = root / "runs/v7/execution"
            run_root.mkdir(parents=True)
            (root / "runs/v7/shadow").mkdir(parents=True)
            (root / "config/paper_v7.json").write_text(json.dumps({
                "paper_only": True, "authenticated_execution": False, "v7": {}
            }), encoding="utf-8")
            (root / "config/live_champion.json").write_text(json.dumps({
                "enabled": True,
                "version": 7,
                "loop": "scripts/paper_v7_loop.sh",
                "config": "config/paper_v7.json",
                "run_root": "runs/v7",
                "deployment_ref": "paper-validated",
                "paper_only": True,
                "authenticated_execution": False,
            }), encoding="utf-8")
            (root / "runs/v7/v7_supervisor.json").write_text(json.dumps({
                "timestamp": int(time.time()), "execution_alive": True, "shadow_alive": True
            }), encoding="utf-8")
            (run_root / "runtime_status.json").write_text(json.dumps({
                "timestamp": int(time.time()), "version": 7, "paper_only": True,
                "authenticated_execution": False, "equity": 10010, "pnl": 10,
                "realized_pnl": 4, "drawdown": 0.01, "gross_exposure": 500,
                "starting_capital": 10000, "killed": False,
                "strategies": {"micro_taker": {"signals": 9, "best_edge": 0.002}},
            }), encoding="utf-8")
            (run_root / "allocator_status.json").write_text(json.dumps({
                "paper_only": True, "authenticated_execution": False
            }), encoding="utf-8")
            with (run_root / "strategy_status.csv").open("w", newline="", encoding="utf-8") as handle:
                writer = csv.DictWriter(handle, fieldnames=["name","equity","pnl","realized_pnl","fills","open_positions","gross_exposure","alive","killed"])
                writer.writeheader()
                writer.writerow({"name":"micro_taker","equity":2010,"pnl":10,"realized_pnl":4,"fills":2,"open_positions":1,"gross_exposure":50,"alive":1,"killed":0})
            (root / "runs/v7/shadow/cross_sectional_rank.json").write_text(json.dumps({"forward":[{
                "horizon_minutes":15,"completed_sections":20,"mean_rank_ic":0.03,"mean_top_bottom_logit_spread":0.01
            }]}), encoding="utf-8")
            (root / "scripts").mkdir()
            (root / "scripts/paper_v7_loop.sh").write_text("#!/bin/sh\n", encoding="utf-8")
            health, metrics = mod.V7Collector(root, Path("config/live_champion.json"), 180).snapshot()
            self.assertTrue(health["ok"])
            self.assertIn("polymarket_v7_runtime_pnl_usd 10", metrics)
            self.assertIn('polymarket_v7_strategy_pnl_usd{strategy="micro_taker"} 10', metrics)
            self.assertIn('polymarket_v7_strategy_pnl_per_fill_usd{strategy="micro_taker"} 5', metrics)
            self.assertIn('polymarket_v7_rank_mean_ic{horizon_minutes="15"} 0.03', metrics)
            self.assertIn("polymarket_v7_runtime_authenticated_execution 0", metrics)

    def test_health_manifest_contract_rejects_non_v7_and_accepts_safe_v7(self) -> None:
        mod = load_module("v7_server_health_fixture", ROOT / "scripts/v7_server_health.py")
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "config").mkdir(); (root / "scripts").mkdir(); (root / "runs").mkdir()
            (root / "scripts/paper_v7_loop.sh").write_text("#!/bin/sh\n", encoding="utf-8")
            (root / "config/paper_v7.json").write_text(json.dumps({
                "paper_only": True, "authenticated_execution": False, "v7": {}
            }), encoding="utf-8")
            manifest = {
                "enabled": True, "version": 7, "loop": "scripts/paper_v7_loop.sh",
                "config": "config/paper_v7.json", "run_root": "runs/v7",
                "deployment_ref": "paper-validated", "paper_only": True,
                "authenticated_execution": False,
            }
            path = root / "config/live_champion.json"
            path.write_text(json.dumps(manifest), encoding="utf-8")
            loaded = mod.validate_manifest(root, path)
            self.assertEqual(loaded["version"], 7)
            manifest["version"] = 6
            path.write_text(json.dumps(manifest), encoding="utf-8")
            with self.assertRaisesRegex(RuntimeError, "V7"):
                mod.validate_manifest(root, path)

    def test_dashboard_and_compose_are_v7_only(self) -> None:
        dashboard = json.loads((ROOT / "monitoring/grafana/dashboards/polymarket-v7.json").read_text(encoding="utf-8"))
        self.assertEqual(dashboard["uid"], "polymarket-v7")
        titles = {panel["title"] for panel in dashboard["panels"]}
        for title in ("Total equity", "Total PnL", "Realized PnL", "PnL by strategy", "Fills by strategy", "Ranking IC by horizon"):
            self.assertIn(title, titles)
        compose = (ROOT / "docker-compose.monitoring.yml").read_text(encoding="utf-8")
        self.assertIn("monitoring/v7_exporter.py", compose)
        self.assertIn("127.0.0.1:${GRAFANA_PORT:-3000}:3000", compose)
        self.assertIn("GRAFANA_ADMIN_PASSWORD:?", compose)
        self.assertNotIn("exporter_v6", compose)


if __name__ == "__main__":
    unittest.main()
