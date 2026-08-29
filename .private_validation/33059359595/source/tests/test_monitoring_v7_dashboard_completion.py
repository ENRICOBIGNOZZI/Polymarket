from __future__ import annotations

import json
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
DASHBOARD = ROOT / "monitoring/grafana/dashboards/polymarket-v7.json"


class V7DashboardCompletionTest(unittest.TestCase):
    def test_dashboard_uses_canonical_ledger_completion_metrics(self) -> None:
        dashboard = json.loads(DASHBOARD.read_text(encoding="utf-8"))
        serialized = json.dumps(dashboard)
        for metric in (
            "polymarket_execution_complete_fills",
            "polymarket_execution_partial_fills",
            "polymarket_execution_unwinds",
            "polymarket_execution_capital_hours",
            "polymarket_execution_final_pnl_usd",
            "polymarket_v7_ledger_valid",
            "polymarket_execution_mean_markout",
        ):
            self.assertIn(metric, serialized)
        self.assertIn("Completion Rate", serialized)
        self.assertIn("Canonical Ledger / Completion", serialized)


if __name__ == "__main__":
    unittest.main()
