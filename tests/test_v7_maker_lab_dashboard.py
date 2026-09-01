from __future__ import annotations

import unittest
from pathlib import Path


class DashboardContractTests(unittest.TestCase):
    def test_research_maker_lab_is_not_provisioned_as_a_live_dashboard(self):
        path = Path(__file__).resolve().parents[1] / "monitoring" / "grafana" / "dashboards" / "polymarket-v7-maker-lab.json"
        self.assertFalse(path.exists())


if __name__ == "__main__":
    unittest.main()
