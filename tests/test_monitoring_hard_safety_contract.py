from __future__ import annotations

import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


class MonitoringHardSafetyContractTests(unittest.TestCase):
    def test_v7_hard_drawdown_limit_has_independent_critical_alert(self) -> None:
        alerts = (ROOT / "monitoring" / "prometheus" / "alerts.yml").read_text(encoding="utf-8")
        expected = """      - alert: PolymarketV7HardDrawdownLimitBreach
        expr: polymarket_runtime_drawdown_ratio >= 0.15
        for: 15s
        labels:
          severity: critical
"""
        self.assertIn(expected, alerts)
        self.assertNotIn("PolymarketV5", alerts)
        self.assertNotIn("PolymarketV6", alerts)
        self.assertNotIn("legacy", alerts.lower())

    def test_monitoring_workflow_runs_v7_exporter_and_hard_safety_tests(self) -> None:
        workflow = (ROOT / ".github" / "workflows" / "monitoring.yml").read_text(encoding="utf-8")
        self.assertIn("tests/test_monitoring_v7_exporter.py", workflow)
        self.assertIn("tests/test_monitoring_hard_safety_contract.py", workflow)
        self.assertIn("tests/test_grafana_v7_contract.py", workflow)
        for forbidden in ("test_monitoring_v4", "test_monitoring_v5", "test_monitoring_v6", "paper_v6"):
            self.assertNotIn(forbidden, workflow)


if __name__ == "__main__":
    unittest.main()
