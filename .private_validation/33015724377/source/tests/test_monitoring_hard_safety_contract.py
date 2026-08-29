from __future__ import annotations

import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


class MonitoringHardSafetyContractTests(unittest.TestCase):
    def test_hard_drawdown_limit_has_independent_critical_alert(self) -> None:
        alerts = (ROOT / "monitoring" / "prometheus" / "alerts.yml").read_text(encoding="utf-8")
        expected = """      - alert: PolymarketV6HardDrawdownLimitBreach
        expr: polymarket_runtime_drawdown_ratio >= polymarket_allocator_global_max_drawdown_ratio
        for: 15s
        labels:
          severity: critical
"""
        self.assertIn(expected, alerts)

    def test_monitoring_workflow_runs_v6_exporter_and_hard_safety_tests(self) -> None:
        workflow = (ROOT / ".github" / "workflows" / "monitoring.yml").read_text(encoding="utf-8")
        self.assertIn("tests/test_monitoring_v6_exporter.py", workflow)
        self.assertIn("tests/test_monitoring_hard_safety_contract.py", workflow)


if __name__ == "__main__":
    unittest.main()
