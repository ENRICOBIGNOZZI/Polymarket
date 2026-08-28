from __future__ import annotations

import json
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


class V7OperatorHomeTest(unittest.TestCase):
    def test_one_dashboard_is_the_complete_operator_home(self) -> None:
        manifest = json.loads((ROOT / "monitoring/v7_monitoring_manifest.json").read_text())
        dashboard = json.loads((ROOT / manifest["grafana"]["dashboard_file"]).read_text())
        self.assertTrue(manifest["grafana"]["canonical_operator_home"])
        self.assertEqual(dashboard["uid"], "polymarket-v7")
        self.assertIn("Operator Home", dashboard["title"])
        serialized = json.dumps(dashboard)
        for metric in (
            "polymarket_v7_supervisor_alive",
            "polymarket_v7_single_writer_ok",
            "polymarket_v7_exact_sha_ok",
            "polymarket_v7_runtime_uptime_seconds",
            "polymarket_v7_component_ready",
            "polymarket_v7_restart_count_window",
            "polymarket_v7_disk_free_ratio",
            "polymarket_execution_candidates",
            "polymarket_execution_makes",
            "polymarket_execution_takes",
            "polymarket_execution_arbs",
            "polymarket_execution_cancels",
            "polymarket_execution_withdraws",
            "polymarket_execution_effective_orders",
            "polymarket_v7_latency_stage_nanoseconds",
            "ALERTS",
        ):
            self.assertIn(metric, serialized)
        self.assertIn("no aggregate P0 claim", serialized)
        self.assertIn("polymarket_external_fair_present * polymarket_external_fair_healthy", serialized)

    def test_alert_catalog_has_only_meaningful_operational_failures(self) -> None:
        catalog = json.loads((ROOT / "config/v7_runtime_alerts.json").read_text())
        alerts = (ROOT / "monitoring/v7_alerts.yml").read_text()
        self.assertEqual(catalog["policy"], "meaningful_failures_only")
        self.assertTrue(catalog["suppress_zero_trade_windows"])
        for alert in (
            "PolymarketV7ExecutionOwnerDown",
            "PolymarketV7DuplicateWriter",
            "PolymarketV7CanonicalLedgerUnavailable",
            "PolymarketV7KillSwitchEngaged",
            "PolymarketV7GrafanaDown",
            "PolymarketV7DiskPressure",
            "PolymarketV7RestartStorm",
            "PolymarketV7ExactShaDrift",
        ):
            self.assertIn(alert, alerts)
        self.assertNotIn("ZeroTrades", alerts)
        self.assertNotIn("NoFills", alerts)

    def test_native_service_templates_preserve_exact_sha_and_full_paper(self) -> None:
        files = [
            ROOT / "ops/systemd/polymarket-v7-paper.service.in",
            ROOT / "ops/launchd/com.polymarket.v7.paper.plist.in",
        ]
        for path in files:
            text = path.read_text()
            self.assertIn("@EXPECTED_SHA@", text)
            self.assertIn("PM_V7_AUTHENTICATED_EXECUTION", text)
            self.assertIn("PM_V7_REAL_ORDER_SUBMISSION", text)
        systemd = files[0].read_text()
        self.assertIn("Restart=on-failure", systemd)
        self.assertIn("StartLimitBurst=3", systemd)
        launchd = files[1].read_text()
        self.assertIn("KeepAlive", launchd)
        self.assertIn("SuccessfulExit", launchd)


if __name__ == "__main__":
    unittest.main()
