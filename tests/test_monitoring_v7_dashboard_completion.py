from __future__ import annotations

import json
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


class V7DashboardCompletionTest(unittest.TestCase):
    def test_dashboard_uses_canonical_economic_units_for_completion(self) -> None:
        dashboard = json.loads((ROOT / "monitoring/grafana/dashboards/polymarket-v7.json").read_text(encoding="utf-8"))
        serialized = json.dumps(dashboard)
        self.assertIn("Economic Completion Rate", serialized)
        self.assertIn("polymarket_v7_canonical_complete_units / clamp_min(polymarket_v7_canonical_submitted_units, 1)", serialized)
        self.assertNotIn("polymarket_strategy_fill_rate", serialized)
        self.assertNotIn("polymarket_execution_complete_fills / clamp_min(polymarket_execution_fills, 1)", serialized)

    def test_dashboard_preserves_execution_event_diagnostics_without_calling_them_completion(self) -> None:
        dashboard = json.loads((ROOT / "monitoring/grafana/dashboards/polymarket-v7.json").read_text(encoding="utf-8"))
        serialized = json.dumps(dashboard)
        self.assertIn("polymarket_execution_fills", serialized)
        self.assertIn("polymarket_execution_partial_fills", serialized)
        self.assertIn("polymarket_execution_unwinds", serialized)
        self.assertIn("polymarket_execution_mean_markout", serialized)
        self.assertIn("complete leg fills", serialized)


if __name__ == "__main__":
    unittest.main()
