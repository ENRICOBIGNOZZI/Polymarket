from __future__ import annotations

import json
import unittest
from pathlib import Path


class DashboardContractTests(unittest.TestCase):
    def test_dashboard_contract(self):
        path = Path(__file__).resolve().parents[1] / "monitoring" / "grafana" / "dashboards" / "polymarket-v7-maker-lab.json"
        dashboard = json.loads(path.read_text(encoding="utf-8"))
        self.assertEqual(dashboard["uid"], "polymarket-v7-maker-lab")
        self.assertIn("Microstructure", dashboard["title"])
        raw = path.read_text(encoding="utf-8")
        for metric in (
            "polymarket_maker_lab_segment_filled_orders",
            "polymarket_maker_lab_segment_markout_pnl_usd",
            "polymarket_maker_lab_conditional_markout_pnl_usd",
            "polymarket_maker_lab_market_realized_pnl_usd",
            "polymarket_v7_maker_cohort_supervisor_ready",
            "polymarket_v7_maker_cohort_rotations_total",
            "polymarket_v7_maker_rotation_blocked_nonflat",
            'dimension=\\"lifetime_arm\\"',
        ):
            self.assertIn(metric, raw)
        for word in (
            "toxicity", "queue", "ofi", "inventory", "spread", "imbalance", "latency",
            "reward", "15s control", "60s persistent",
        ):
            self.assertIn(word, raw.lower())


if __name__ == "__main__":
    unittest.main()
