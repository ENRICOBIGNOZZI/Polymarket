from __future__ import annotations

import importlib.util
import json
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
EXPORTER_PATH = ROOT / "monitoring" / "exporter_v7.py"
SPEC = importlib.util.spec_from_file_location("exporter_v7", EXPORTER_PATH)
assert SPEC and SPEC.loader
exporter = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = exporter
SPEC.loader.exec_module(exporter)


class V7NativeMonitoringTest(unittest.TestCase):
    @staticmethod
    def _write(path: Path, value: dict) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(value), encoding="utf-8")

    def _fixture(self, root: Path, *, now: int = 1_000) -> None:
        self._write(
            root / "v7_supervisor.json",
            {"timestamp": now - 5, "execution_alive": True, "shadow_alive": True},
        )
        self._write(
            root / "execution" / "v7_execution_supervisor.json",
            {"timestamp": now - 10, "paper_only": True, "authenticated_execution": False},
        )
        self._write(
            root / "execution" / "runtime_status.json",
            {
                "schema": "polymarket_v7_runtime_status_v1",
                "timestamp": now - 10,
                "version": 7,
                "paper_only": True,
                "authenticated_execution": False,
                "starting_capital": 10_000,
                "cash": 9_400,
                "equity": 10_100,
                "pnl": 100,
                "realized_pnl": 70,
                "drawdown": 0.05,
                "gross_exposure": 1_200,
                "live_units": 3,
                "killed": False,
                "strategies": {
                    "micro_taker": {
                        "equity": 1_250,
                        "pnl": 50,
                        "fills": 4,
                        "live_units": 1,
                        "signals": 12,
                        "best_edge": 0.002,
                        "killed": False,
                    }
                },
            },
        )
        self._write(
            root / "execution" / "market_proxy_status.json",
            {
                "schema": "polymarket_v7_market_proxy_status_v1",
                "timestamp": now - 10,
                "paper_only": True,
                "upstream_gamma_ok": True,
                "markets": 200,
                "failures": 0,
            },
        )
        self._write(
            root / "execution" / "v7_execution_evidence.json",
            {
                "schema": "polymarket_v7_execution_evidence_v1",
                "timestamp": now - 15,
                "paper_only": True,
                "summary": {
                    "paper_eligible_models": 1,
                    "insufficient_evidence_models": 2,
                },
                "models": {
                    "micro_taker": {
                        "orders_submitted": 10,
                        "fills": 4,
                        "fill_rate": 0.4,
                        "net_pnl": 7.0,
                        "stressed_net_pnl": 3.0,
                        "forward_markout_observations": 4,
                        "mean_forward_markout": 0.001,
                        "paper_eligible": True,
                    }
                },
            },
        )
        self._write(
            root / "shadow" / "scheduler_status.json",
            {
                "timestamp": now - 5,
                "paper_only": True,
                "authenticated_execution": False,
                "last_started": {"pca_stat_arb": now - 60, "cross_sectional_rank": now - 30},
            },
        )

    def test_healthy_v7_fixture_exports_economics_execution_and_freshness(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            run_root = Path(directory) / "paper_v7_live"
            self._fixture(run_root)
            snapshot = exporter.collect_snapshot(run_root, ROOT, now=1_000)
            self.assertEqual(exporter.health_reasons(snapshot), [])
            metrics = exporter.render_prometheus(snapshot)
            self.assertIn("polymarket_v7_runtime_info 1", metrics)
            self.assertIn('polymarket_runtime_info{adapter="v7_native",run_root="paper_v7_live",version="v7"} 1', metrics)
            self.assertIn("polymarket_v7_operator_authority_valid 1", metrics)
            self.assertIn("polymarket_v7_authority_max_drawdown_ratio 0.15", metrics)
            self.assertIn("polymarket_v7_paper_only_contract_ok 1", metrics)
            self.assertIn("polymarket_v7_authenticated_execution_disabled 1", metrics)
            self.assertIn("polymarket_runtime_pnl_usd 100", metrics)
            self.assertIn("polymarket_runtime_realized_pnl_usd 70", metrics)
            self.assertIn("polymarket_runtime_unrealized_executable_pnl_usd 30", metrics)
            self.assertIn('polymarket_strategy_fill_rate{strategy="micro_taker"} 0.4', metrics)
            self.assertIn('polymarket_strategy_mean_markout{strategy="micro_taker"} 0.001', metrics)
            self.assertIn('polymarket_v7_shadow_job_age_seconds{job="cross_sectional_rank"} 30', metrics)

    def test_stale_execution_evidence_fails_health_closed(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            run_root = Path(directory) / "paper_v7_live"
            self._fixture(run_root)
            path = run_root / "execution" / "v7_execution_evidence.json"
            value = json.loads(path.read_text(encoding="utf-8"))
            value["timestamp"] = 700
            self._write(path, value)
            snapshot = exporter.collect_snapshot(run_root, ROOT, now=1_000)
            self.assertIn("evidence_stale", exporter.health_reasons(snapshot))

    def test_authenticated_runtime_fails_health_closed(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            run_root = Path(directory) / "paper_v7_live"
            self._fixture(run_root)
            path = run_root / "execution" / "runtime_status.json"
            value = json.loads(path.read_text(encoding="utf-8"))
            value["authenticated_execution"] = True
            self._write(path, value)
            snapshot = exporter.collect_snapshot(run_root, ROOT, now=1_000)
            self.assertIn("authenticated_execution_not_disabled", exporter.health_reasons(snapshot))

    def test_authenticated_execution_supervisor_fails_health_closed(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            run_root = Path(directory) / "paper_v7_live"
            self._fixture(run_root)
            path = run_root / "execution" / "v7_execution_supervisor.json"
            value = json.loads(path.read_text(encoding="utf-8"))
            value["authenticated_execution"] = True
            self._write(path, value)
            snapshot = exporter.collect_snapshot(run_root, ROOT, now=1_000)
            self.assertIn(
                "execution_supervisor_authenticated_execution_not_disabled",
                exporter.health_reasons(snapshot),
            )

    def test_killed_runtime_fails_health_closed(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            run_root = Path(directory) / "paper_v7_live"
            self._fixture(run_root)
            path = run_root / "execution" / "runtime_status.json"
            value = json.loads(path.read_text(encoding="utf-8"))
            value["killed"] = True
            self._write(path, value)
            snapshot = exporter.collect_snapshot(run_root, ROOT, now=1_000)
            self.assertIn("runtime_killed", exporter.health_reasons(snapshot))

    def test_drawdown_at_master_limit_fails_health_closed(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            run_root = Path(directory) / "paper_v7_live"
            self._fixture(run_root)
            path = run_root / "execution" / "runtime_status.json"
            value = json.loads(path.read_text(encoding="utf-8"))
            value["drawdown"] = 0.15
            self._write(path, value)
            snapshot = exporter.collect_snapshot(run_root, ROOT, now=1_000)
            self.assertIn("drawdown_limit_breached", exporter.health_reasons(snapshot))

    def test_monitoring_manifest_and_dashboard_are_single_v7_contract(self) -> None:
        manifest_path = ROOT / "monitoring" / "v7_monitoring_manifest.json"
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        dashboard_path = ROOT / manifest["grafana"]["dashboard_file"]
        dashboard = json.loads(dashboard_path.read_text(encoding="utf-8"))
        self.assertEqual(manifest["version"], 7)
        self.assertTrue(manifest["paper_only"])
        self.assertFalse(manifest["authenticated_execution"])
        self.assertEqual(manifest["run_root"], "runs/paper_v7_live")
        self.assertEqual(manifest["prometheus"]["alert_rules"], "monitoring/v7_alerts.yml")
        self.assertEqual(dashboard["uid"], manifest["grafana"]["dashboard_uid"])
        self.assertEqual(dashboard["uid"], "polymarket-v7")
        self.assertIn("Polymarket V7", dashboard["title"])
        serialized = json.dumps(dashboard)
        for metric in (
            "polymarket_runtime_equity_usd",
            "polymarket_runtime_pnl_usd",
            "polymarket_runtime_drawdown_ratio",
            "polymarket_strategy_pnl_usd",
            "polymarket_strategy_fill_rate",
            "polymarket_strategy_mean_markout",
            "polymarket_v7_state_age_seconds",
        ):
            self.assertIn(metric, serialized)

    def test_prometheus_alerts_cover_v7_hard_safety_visibility(self) -> None:
        config = (ROOT / "monitoring/prometheus_v7.yml").read_text(encoding="utf-8")
        alerts = (ROOT / "monitoring/v7_alerts.yml").read_text(encoding="utf-8")
        self.assertIn("__POLYMARKET_V7_ALERT_RULES__", config)
        for required in (
            "PolymarketV7ExporterDown",
            "PolymarketV7RuntimeContractInvalid",
            "PolymarketV7PaperContractInvalid",
            "PolymarketV7AuthenticatedExecutionEnabled",
            "PolymarketV7KillSwitchEngaged",
            "PolymarketV7HardDrawdownLimitBreach",
            "PolymarketV7ExecutionOwnerDown",
            "PolymarketV7StateMissing",
            "PolymarketV7RuntimeStateStale",
            "PolymarketV7MarketProxyEmpty",
            "PolymarketV7CanonicalLedgerInvalid",
            "polymarket_v7_authority_max_drawdown_ratio",
        ):
            self.assertIn(required, alerts)

    def test_monitoring_sources_have_no_retired_exporter_dependency(self) -> None:
        exporter_source = EXPORTER_PATH.read_text(encoding="utf-8").lower()
        self.assertNotIn("exporter_v6", exporter_source)
        self.assertNotIn("paper_latest", exporter_source)
        provider = (ROOT / "monitoring/grafana/provisioning/dashboards/v7.yml").read_text(encoding="utf-8")
        self.assertNotIn("/Users/enrico", provider)
        self.assertIn("__POLYMARKET_V7_DASHBOARD_DIR__", provider)
        installer = (ROOT / "ops/apply_v7_monitoring_config_macos.sh").read_text(encoding="utf-8")
        self.assertIn("v7_monitoring_manifest.json", installer)
        self.assertIn("prometheus-v7-alerts.yml", installer)
        self.assertNotIn("exporter_latest", installer)
        self.assertNotIn("paper_v6", installer)

    def test_monitoring_workflow_executes_v7_monitoring_contract(self) -> None:
        workflow = (ROOT / ".github/workflows/monitoring.yml").read_text(encoding="utf-8")
        for required in (
            "tests/test_monitoring_v7_native.py",
            "tests/test_monitoring_v7_ledger.py",
            "tests/test_monitoring_v7_dashboard_completion.py",
            "monitoring/exporter_v7.py",
            "monitoring/v7_ledger_metrics.py",
            "monitoring/v7_monitoring_manifest.json",
            "monitoring/v7_alerts.yml",
            "monitoring/grafana/dashboards/polymarket-v7.json",
            "ops/apply_v7_monitoring_config_macos.sh",
        ):
            self.assertIn(required, workflow)


if __name__ == "__main__":
    unittest.main()
