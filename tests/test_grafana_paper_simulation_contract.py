from __future__ import annotations

import json
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
DASHBOARD = ROOT / "monitoring" / "grafana" / "dashboards" / "polymarket-latest.json"


class GrafanaPaperSimulationContractTest(unittest.TestCase):
    def setUp(self) -> None:
        self.dashboard = json.loads(DASHBOARD.read_text(encoding="utf-8"))
        self.panels = {panel.get("title"): panel for panel in self.dashboard.get("panels", [])}

    def test_dashboard_distinguishes_live_paper_from_oos_validation(self) -> None:
        self.assertEqual(self.dashboard.get("title"), "Polymarket — Live Paper Simulation")
        for title in (
            "Live Paper Bundles",
            "Reserved Cash",
            "Maker Fill Events",
            "Resting Hedge Bundles",
            "Closed / Unwound Baskets",
            "Public Trade Tape Rows",
            "OOS Trades — Validation",
        ):
            self.assertIn(title, self.panels)

    def test_live_paper_panels_use_execution_metrics(self) -> None:
        self.assertEqual(
            self.panels["Maker Fill Events"]["targets"][0]["expr"],
            "sum(polymarket_maker_fills_total)",
        )
        self.assertIn(
            'status="RESTING"',
            self.panels["Resting Hedge Bundles"]["targets"][0]["expr"],
        )
        self.assertEqual(
            self.panels["OOS Trades — Validation"]["targets"][0]["expr"],
            "polymarket_runtime_oos_trades",
        )

    def test_dashboard_explains_zero_fill_state(self) -> None:
        text = self.panels["How to read this dashboard"]["options"]["content"]
        self.assertIn("Paper simulation", text)
        self.assertIn("waiting for evidence of a fill", text)
        self.assertIn("not", text.lower())
        self.assertIn("OOS", text)


if __name__ == "__main__":
    unittest.main()
