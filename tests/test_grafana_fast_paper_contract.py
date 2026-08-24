from __future__ import annotations

import json
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
DASHBOARD = ROOT / "monitoring" / "grafana" / "dashboards" / "polymarket-fast-paper.json"


class GrafanaFastPaperContractTest(unittest.TestCase):
    def setUp(self) -> None:
        self.dashboard = json.loads(DASHBOARD.read_text(encoding="utf-8"))
        self.panels = {panel.get("title"): panel for panel in self.dashboard.get("panels", [])}

    def test_fast_dashboard_surfaces_taker_pnl_positions_and_fills(self) -> None:
        self.assertEqual(self.dashboard.get("title"), "Polymarket — Fast Paper PnL")
        self.assertEqual(self.dashboard.get("uid"), "polymarket-fast-paper")
        expected = {
            "Taker Marked PnL": "polymarket_terminal_pnl_usd",
            "Taker Equity": "polymarket_terminal_equity_usd",
            "Taker Open Positions": "polymarket_terminal_open_positions",
            "Taker Fill Events": "polymarket_terminal_fills_total",
            "Taker Gross Exposure": "polymarket_terminal_gross_exposure_usd",
            "Taker Data Staleness": "polymarket_terminal_staleness_seconds",
        }
        for title, metric in expected.items():
            self.assertIn(title, self.panels)
            expr = self.panels[title]["targets"][0]["expr"]
            self.assertIn(metric, expr)
            self.assertIn("or vector(0)", expr)

    def test_paper_sleeves_are_not_accounting_merged(self) -> None:
        self.assertIn("Multi-leg Marked PnL", self.panels)
        self.assertIn("Maker Fill Events", self.panels)
        text = self.panels["Execution semantics"]["options"]["content"]
        self.assertIn("distinct paper sleeves", text)
        self.assertIn("No authenticated real-money order submission", text)
        self.assertIn("fees, slippage and model-uncertainty penalty", text)


if __name__ == "__main__":
    unittest.main()
