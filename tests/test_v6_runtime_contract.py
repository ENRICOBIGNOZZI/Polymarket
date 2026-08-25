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

    def test_v6_runtime_exists_with_manifest_selected_paper_champion(self) -> None:
        champion = json.loads((ROOT / "config/live_champion.json").read_text())
        self.assertIn(int(champion["version"]), (5, 6))
        if int(champion["version"]) == 6:
            self.assertEqual(champion["loop"], "scripts/paper_v6_loop.sh")
            self.assertEqual(champion["config"], "config/paper_v6.json")
            self.assertEqual(champion["run_root"], "runs/paper_v6_live")
        else:
            self.assertEqual(champion["loop"], "scripts/paper_v5_loop.sh")
            self.assertEqual(champion["config"], "config/paper_v5.json")
        self.assertTrue((ROOT / "scripts/paper_v6_loop.sh").is_file())
        self.assertTrue((ROOT / "config/paper_v6.json").is_file())
        architecture = json.loads((ROOT / "config/v6_model_architecture.json").read_text())
        self.assertEqual(architecture["version"], 6)
        self.assertTrue(architecture["paper_only"])
        self.assertFalse(architecture["allow_authenticated_execution"])

    def test_capital_and_hard_safety_contracts(self) -> None:
        cfg = json.loads((ROOT / "config/paper_v6.json").read_text())
        v6 = cfg["v6"]
        total = sum(float(v6[key]) for key in (
            "micro_maker_capital_fraction",
            "micro_taker_capital_fraction",
            "relative_value_capital_fraction",
            "hard_arb_capital_fraction",
            "external_capital_fraction",
            "reserve_fraction",
        ))
        self.assertTrue(math.isclose(total, 1.0, rel_tol=0.0, abs_tol=1e-12))
        self.assertGreater(float(v6["hard_arb_capital_fraction"]), 0.0)
        self.assertEqual(float(cfg["max_drawdown"]), 0.15)
        self.assertEqual(float(cfg["max_market_fraction"]), 0.025)
        self.assertEqual(float(cfg["max_event_fraction"]), 0.08)
        self.assertEqual(float(cfg["max_gross_fraction"]), 0.45)
        self.assertIs(cfg["multi_strategy"]["paper_only"], True)
        self.assertEqual(float(cfg["multi_strategy"]["global_max_drawdown"]), 0.15)
        self.assertEqual(float(cfg["multi_strategy"]["global_max_gross_fraction"]), 0.45)
        self.assertEqual(float(cfg["semantic_shrink"]), 0.0)

    def test_threshold_parser_recognizes_nested_crypto_contracts(self) -> None:
        low = self.relations.threshold_signature("Will Bitcoin reach $82,500 in August 2026?")
        high = self.relations.threshold_signature("Will Bitcoin reach $90,000 in August 2026?")
        self.assertIsNotNone(low); self.assertIsNotNone(high)
        self.assertEqual(low[0], high[0]); self.assertEqual(low[1], "UP"); self.assertLess(low[2], high[2])

    def test_local_factor_cluster_is_not_one_market_pca(self) -> None:
        family1 = self.local_factor.payoff_family("Will Bitcoin reach $82,500 in August 2026?")
        family2 = self.local_factor.payoff_family("Will Bitcoin reach $90,000 in August 2026?")
        self.assertEqual(family1, family2); self.assertIsNotNone(family1)

    def test_bh_cutoff_controls_multiple_reversion_tests(self) -> None:
        self.assertAlmostEqual(self.local_factor.bh_cutoff([0.001, 0.02, 0.20, 0.80], 0.10), 0.02)
        self.assertEqual(self.local_factor.bh_cutoff([0.08, 0.20, 0.80], 0.05), 0.0)

    def test_ar_fit_requires_actual_mean_reversion(self) -> None:
        innovations = [0.04, -0.025, 0.015, -0.035, 0.02, 0.005, -0.01]
        residual = [0.7]
        for i in range(1, 100):
            residual.append(0.65 * residual[-1] + innovations[i % len(innovations)])
        phi, tstat, _, sd = self.local_factor.ar_fit(residual)
        self.assertGreater(sd, 0.0)
        self.assertGreater(phi, 0.02)
        self.assertLess(phi, 0.999)
        self.assertLess(tstat, 0.0)

    def test_v6_execution_excludes_global_pca_semantic_and_weak_b1(self) -> None:
        loop = (ROOT / "scripts/paper_v6_loop.sh").read_text()
        self.assertNotIn("polymarket_pca_stat_arb", loop)
        self.assertNotIn("strategies/semantic", loop)
        self.assertNotIn("build_v4_intents.py --strategy B1", loop)
        self.assertNotIn('--input "$RUN_ROOT/b1_intents.csv"', loop)
        self.assertIn("stat_arb_pairs_diagnostic.csv", loop)
        self.assertIn("--min-t-reversion 2.00", loop)
        self.assertIn("--fdr 0.10", loop)
        self.assertIn("--min-common-points 48", loop)
        self.assertIn("v6_local_factor_intents.py", loop)
        self.assertIn("v6_relation_intents.py", loop)
        self.assertIn("v6_micro_taker.py", loop)
        self.assertIn("v6_hard_arb_paper.py", loop)
        self.assertIn("polymarket_maker_paper", loop)

    def test_v6_materializes_external_feed_before_external_engine_starts(self) -> None:
        cfg = json.loads((ROOT / "config/paper_v6.json").read_text())
        loop = (ROOT / "scripts/paper_v6_loop.sh").read_text()
        self.assertEqual(cfg["external_signals_file"], "runs/paper_v6_live/external_signals.csv")
        self.assertIn("refresh_external_feed(){", loop)
        self.assertIn("v6_external_bridge.py", loop)
        self.assertIn("refresh_external_feed;start_external", loop)
        self.assertLess(loop.index("refresh_external_feed;start_external"), loop.index("while true;do"))
        self.assertIn("external_bridge_status.json", loop)
        self.assertIn("market_key,q_yes,confidence,source,timestamp", loop)


if __name__ == "__main__":
    unittest.main()
