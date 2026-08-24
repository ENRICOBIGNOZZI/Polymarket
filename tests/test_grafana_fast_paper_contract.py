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

    def test_dashboard_surfaces_live_equivalent_pnl_account(self) -> None:
        self.assertEqual(self.dashboard.get("title"), "Polymarket — Simulated Live PnL")
        self.assertEqual(self.dashboard.get("uid"), "polymarket-fast-paper")
        expected = {
            "SIMULATED LIVE PNL": "polymarket_terminal_pnl_usd",
            "Simulated Equity": "polymarket_terminal_equity_usd",
            "Realized PnL": "polymarket_terminal_realized_pnl_usd",
            "Unrealized PnL": "polymarket_terminal_unrealized_pnl_usd",
            "Simulated Fill Events": "polymarket_terminal_fills_total",
            "Simulator State": "polymarket_terminal_state_present",
            "Open Positions": "polymarket_terminal_open_positions",
            "Gross Exposure": "polymarket_terminal_gross_exposure_usd",
            "Protocol Fees": "polymarket_terminal_fees_usd_total",
            "Data Staleness": "polymarket_terminal_staleness_seconds",
        }
        for title, metric in expected.items():
            self.assertIn(title, self.panels)
            expr = self.panels[title]["targets"][0]["expr"]
            self.assertIn(metric, expr)

    def test_dashboard_distinguishes_true_zero_from_dead_simulator(self) -> None:
        state = self.panels["Simulator State"]
        mappings = state["fieldConfig"]["defaults"]["mappings"][0]["options"]
        self.assertEqual(mappings["0"]["text"], "DOWN")
        self.assertEqual(mappings["1"]["text"], "RUNNING")
        text = self.panels["How to interpret the simulation"]["options"]["content"]
        self.assertIn("Simulator State = RUNNING", text)
        self.assertIn("Data Staleness", text)
        self.assertIn("zero must not be interpreted as trading performance", text)

    def test_secondary_sleeves_are_not_double_counted(self) -> None:
        self.assertIn("Multi-leg Marked PnL", self.panels)
        self.assertIn("Multi-leg Realized PnL", self.panels)
        self.assertIn("Maker Fill Events", self.panels)
        text = self.panels["How to interpret the simulation"]["options"]["content"]
        self.assertIn("not added to the main account", text)
        self.assertIn("avoiding double-counting capital", text)
        self.assertIn("No authenticated real-money order submission", text)


if __name__ == "__main__":
    unittest.main()
