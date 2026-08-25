from __future__ import annotations

import json
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
CONFIG = ROOT / "config" / "paper_v5.json"
CHAMPION = ROOT / "config" / "live_champion.json"


class AggressivePaperActivityContractTest(unittest.TestCase):
    def test_all_five_sleeves_are_enabled_and_capital_is_deployed(self) -> None:
        config = json.loads(CONFIG.read_text(encoding="utf-8"))
        multi = config["multi_strategy"]
        strategies = [row for row in multi["strategies"] if row.get("enabled")]
        self.assertEqual(
            {row["expert"] for row in strategies},
            {"micro", "pca", "graph", "semantic", "external"},
        )
        allocated = sum(float(row["capital_fraction"]) for row in strategies)
        self.assertAlmostEqual(allocated + float(multi["reserve_fraction"]), 1.0)
        self.assertGreaterEqual(allocated, 0.98)
        self.assertLessEqual(float(multi["reserve_fraction"]), 0.02)

    def test_activity_limits_are_aggressive_but_drawdown_is_not_weakened(self) -> None:
        config = json.loads(CONFIG.read_text(encoding="utf-8"))
        multi = config["multi_strategy"]
        self.assertGreaterEqual(int(config["market_limit"]), 1000)
        self.assertLessEqual(float(config["min_liquidity"]), 10.0)
        self.assertGreaterEqual(float(multi["global_max_gross_fraction"]), 0.75)
        self.assertEqual(float(multi["global_max_drawdown"]), 0.15)
        self.assertEqual(float(config["max_drawdown"]), 0.15)
        weighted_gross = 0.0
        for row in multi["strategies"]:
            if not row.get("enabled"):
                continue
            overrides = row["overrides"]
            self.assertLessEqual(float(overrides["min_net_edge"]), 0.00010)
            self.assertGreaterEqual(float(overrides["fractional_kelly"]), 0.25)
            self.assertEqual(float(overrides["max_drawdown"]), 0.15)
            weighted_gross += float(row["capital_fraction"]) * float(
                overrides["max_gross_fraction"]
            )
        self.assertLessEqual(weighted_gross, float(multi["global_max_gross_fraction"]))

    def test_parent_stays_fail_closed_and_execution_stays_paper_only(self) -> None:
        config = json.loads(CONFIG.read_text(encoding="utf-8"))
        self.assertTrue(config["multi_strategy"]["paper_only"])
        self.assertTrue(all(float(value) == 0.0 for value in config["expert_weights"].values()))
        champion = json.loads(CHAMPION.read_text(encoding="utf-8"))
        self.assertEqual(champion["loop"], "scripts/aggressive_paper_v5_loop.sh")
        self.assertEqual(champion["deployment_ref"], "paper-validated")


if __name__ == "__main__":
    unittest.main()
