from __future__ import annotations

import json
import math
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


class AggressiveV5ContractTest(unittest.TestCase):
    def test_config_is_broad_aggressive_and_risk_bounded(self) -> None:
        config = json.loads((ROOT / "config" / "paper_v5.json").read_text())
        self.assertGreaterEqual(config["market_limit"], 500)
        self.assertLessEqual(config["min_liquidity"], 25)
        self.assertGreaterEqual(config["max_spread"], 0.30)
        self.assertEqual(set(config["expert_weights"].values()), {0.0})
        self.assertAlmostEqual(config["max_drawdown"], 0.15)
        multi = config["multi_strategy"]
        self.assertAlmostEqual(multi["global_max_drawdown"], 0.15)
        enabled = [row for row in multi["strategies"] if row.get("enabled", True)]
        self.assertEqual(
            {row["name"] for row in enabled},
            {"micro", "pca", "graph", "semantic", "external"},
        )
        self.assertTrue(math.isclose(
            multi["reserve_fraction"] + sum(row["capital_fraction"] for row in enabled),
            1.0,
            abs_tol=1e-9,
        ))
        self.assertTrue(all(row["overrides"]["market_limit"] >= 500 for row in enabled))
        self.assertTrue(all(row["overrides"]["max_drawdown"] <= 0.15 for row in enabled))
        self.assertLessEqual(
            max(row["overrides"]["min_net_edge"] for row in enabled),
            0.001,
        )

    def test_smoke_and_persistent_runtime_execute_all_sleeves(self) -> None:
        smoke = (ROOT / ".github" / "workflows" / "v4-live-smoke.yml").read_text()
        self.assertIn("--markets 500 --min-liquidity 25 --once", smoke)
        self.assertIn("for model in micro pca graph semantic external", smoke)
        self.assertIn("scripts/external_intelligence.py", smoke)
        self.assertIn("direct_probability_rows", smoke)
        self.assertIn("usable_series", smoke)
        self.assertIn("panel_series", smoke)
        self.assertIn("hedge_pass", smoke)
        self.assertIn("--allow-factor-hedges", smoke)
        self.assertIn("--max-factor-hedge-error 0.80", smoke)

        loop = (ROOT / "scripts" / "paper_v5_loop.sh").read_text()
        self.assertIn('MODEL_MARKETS="${V5_MODEL_MARKETS:-500}"', loop)
        self.assertIn('MIN_LIQUIDITY="${V5_MIN_LIQUIDITY:-25}"', loop)
        self.assertIn("materialize_external_signals.py", loop)
        self.assertIn("--allow-factor-hedges", loop)
        self.assertIn("--max-factor-hedge-error 0.80", loop)

    def test_code_contains_broad_admission_and_asynchronous_models(self) -> None:
        engine = (ROOT / "src" / "engine.cpp").read_text()
        stat = (ROOT / "src" / "stat_arb.cpp").read_text()
        pca = (ROOT / "src" / "pca_stat_arb.cpp").read_text()
        api = (ROOT / "src" / "api.cpp").read_text()
        external = (ROOT / "scripts" / "external_intelligence.py").read_text()
        self.assertIn("model_market_eligible", engine)
        self.assertIn("micro_forecast", engine)
        self.assertIn("asof_level", stat)
        self.assertIn("regression_ok", stat)
        self.assertIn("asof_level", pca)
        self.assertIn("hedge_candidates", pca)
        self.assertIn("history_interval_for_range", api)
        self.assertIn("crypto_threshold_probability", external)
        self.assertIn("direct_probability_markets", external)


if __name__ == "__main__":
    unittest.main()
