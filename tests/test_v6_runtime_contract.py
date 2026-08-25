from __future__ import annotations

import importlib.util
import json
import math
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def load_script(name: str, relative: str):
    spec = importlib.util.spec_from_file_location(name, ROOT / relative)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


class V6RuntimeContractTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.relations = load_script("v6_relation_intents_test", "scripts/v6_relation_intents.py")
        cls.local_factor = load_script("v6_local_factor_intents_test", "scripts/v6_local_factor_intents.py")

    def test_champion_points_to_real_v6_runtime(self) -> None:
        champion = json.loads((ROOT / "config/live_champion.json").read_text())
        self.assertEqual(champion["version"], 6)
        self.assertEqual(champion["loop"], "scripts/paper_v6_loop.sh")
        self.assertEqual(champion["config"], "config/paper_v6.json")
        self.assertEqual(champion["run_root"], "runs/paper_v6_live")
        self.assertTrue((ROOT / champion["loop"]).is_file())
        self.assertTrue((ROOT / champion["config"]).is_file())

    def test_capital_sleeves_sum_to_one(self) -> None:
        cfg = json.loads((ROOT / "config/paper_v6.json").read_text())
        v6 = cfg["v6"]
        total = sum(float(v6[key]) for key in (
            "micro_maker_capital_fraction",
            "micro_taker_capital_fraction",
            "multileg_capital_fraction",
            "external_capital_fraction",
            "reserve_fraction",
        ))
        self.assertTrue(math.isclose(total, 1.0, rel_tol=0.0, abs_tol=1e-12))
        self.assertEqual(float(cfg["max_drawdown"]), 0.15)
        self.assertEqual(float(cfg["semantic_shrink"]), 0.0)

    def test_threshold_parser_recognizes_nested_crypto_contracts(self) -> None:
        low = self.relations.threshold_signature("Will Bitcoin reach $82,500 in August 2026?")
        high = self.relations.threshold_signature("Will Bitcoin reach $90,000 in August 2026?")
        self.assertIsNotNone(low)
        self.assertIsNotNone(high)
        self.assertEqual(low[0], high[0])
        self.assertEqual(low[1], "UP")
        self.assertLess(low[2], high[2])

    def test_local_factor_cluster_is_not_one_market_pca(self) -> None:
        family1 = self.local_factor.payoff_family("Will Bitcoin reach $82,500 in August 2026?")
        family2 = self.local_factor.payoff_family("Will Bitcoin reach $90,000 in August 2026?")
        self.assertEqual(family1, family2)
        self.assertIsNotNone(family1)

    def test_ar_fit_requires_actual_mean_reversion(self) -> None:
        residual = [(-1.0) ** i * (0.8 ** i) for i in range(40)]
        phi, tstat, _, sd = self.local_factor.ar_fit(residual)
        self.assertGreater(sd, 0.0)
        self.assertLess(phi, 0.999)
        self.assertLess(tstat, 0.0)

    def test_live_loop_does_not_run_global_pca_or_semantic_expert(self) -> None:
        loop = (ROOT / "scripts/paper_v6_loop.sh").read_text()
        self.assertNotIn("polymarket_pca_stat_arb", loop)
        self.assertNotIn("strategies/semantic", loop)
        self.assertIn("v6_local_factor_intents.py", loop)
        self.assertIn("v6_relation_intents.py", loop)
        self.assertIn("v6_micro_taker.py", loop)
        self.assertIn("polymarket_maker_paper", loop)


if __name__ == "__main__":
    unittest.main()
