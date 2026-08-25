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
        cls.local_factor = load_script("v6_local_factor_v3_test", "scripts/v6_local_factor_v3.py")

    def test_v6_runtime_exists_with_manifest_selected_paper_champion(self) -> None:
        champion = json.loads((ROOT / "config/live_champion.json").read_text())
        self.assertEqual(int(champion["version"]), 6)
        self.assertEqual(champion["loop"], "scripts/paper_v6_loop.sh")
        self.assertEqual(champion["config"], "config/paper_v6.json")
        self.assertEqual(champion["run_root"], "runs/paper_v6_live")
        self.assertTrue((ROOT / "scripts/paper_v6_loop.sh").is_file())
        architecture = json.loads((ROOT / "config/v6_model_architecture.json").read_text())
        self.assertEqual(architecture["version"], 6)
        self.assertTrue(architecture["paper_only"])
        self.assertFalse(architecture["allow_authenticated_execution"])

    def test_aggressive_paper_profile_and_hard_safety_contracts(self) -> None:
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
        self.assertEqual(float(cfg["min_liquidity"]), 2.0)
        self.assertEqual(float(cfg["min_net_edge"]), 0.00005)
        self.assertEqual(float(cfg["fractional_kelly"]), 0.25)
        self.assertEqual(float(cfg["max_trade_usd"]), 125.0)
        self.assertEqual(float(cfg["max_market_fraction"]), 0.05)
        self.assertEqual(float(cfg["max_event_fraction"]), 0.15)
        self.assertEqual(float(cfg["max_gross_fraction"]), 0.70)
        self.assertEqual(float(cfg["multi_strategy"]["global_max_gross_fraction"]), 0.70)
        self.assertEqual(float(cfg["max_drawdown"]), 0.15)
        self.assertEqual(float(cfg["multi_strategy"]["global_max_drawdown"]), 0.15)
        self.assertIs(cfg["multi_strategy"]["paper_only"], True)
        self.assertEqual(float(cfg["slippage_bps"]), 5.0)
        self.assertEqual(float(cfg["uncertainty_penalty"]), 0.0)
        self.assertEqual(float(cfg["semantic_shrink"]), 0.0)

    def test_runtime_routes_fill_aware_v6_models(self) -> None:
        loop = (ROOT / "scripts/paper_v6_loop.sh").read_text()
        self.assertIn("v6_micro_maker.py", loop)
        self.assertIn("v6_micro_taker_v2.py", loop)
        self.assertIn("v6_hard_arb_paper_v2.py", loop)
        self.assertIn("v6_local_factor_v3.py", loop)
        self.assertIn("v6_queue_filter.py", loop)
        self.assertIn("--min-fill-probability 0.005", loop)
        self.assertIn("--target-fill-probability 0.10", loop)
        self.assertIn("--max-improve-ticks 3", loop)
        self.assertIn("--min-joint-fill-probability 0.000001", loop)
        self.assertIn("--completion-threshold 0.60", loop)
        self.assertIn("--min-edge 0.00005", loop)
        self.assertNotIn("polymarket_maker_paper --config", loop)
        self.assertNotIn("polymarket_pca_stat_arb", loop)
        self.assertNotIn("strategies/semantic", loop)

    def test_local_factor_uses_repaired_inference_contract(self) -> None:
        text = (ROOT / "scripts/v6_local_factor_v3.py").read_text()
        self.assertIn("unit_root_block_pvalue", text)
        self.assertIn("sampled", text)
        self.assertIn("path.append", text)
        self.assertIn("other.market_id != market.market_id", text)
        self.assertIn("horizon_residual_change", text)
        self.assertIn("market_end_ts", text)
        self.assertIn("exit_buffer_seconds", text)
        self.assertNotIn("mean_reversion_score_pvalue", text)

    def test_horizon_residual_change_is_n_step(self) -> None:
        class Signal:
            phi = 0.9
            expected_residual_change = -0.1
        self.assertAlmostEqual(self.local_factor.horizon_residual_change(Signal(), 1), -0.1, places=12)
        self.assertAlmostEqual(self.local_factor.horizon_residual_change(Signal(), 2), -0.19, places=12)

    def test_threshold_parser_recognizes_nested_crypto_contracts(self) -> None:
        low = self.relations.threshold_signature("Will Bitcoin reach $82,500 in August 2026?")
        high = self.relations.threshold_signature("Will Bitcoin reach $90,000 in August 2026?")
        self.assertIsNotNone(low)
        self.assertIsNotNone(high)
        self.assertEqual(low[0], high[0])
        self.assertEqual(low[1], "UP")
        self.assertLess(low[2], high[2])

    def test_v6_research_smoke_preserves_base_live_selector(self) -> None:
        workflow = (ROOT / ".github" / "workflows" / "v6-research-smoke.yml").read_text()
        self.assertIn("fetch-depth: 0", workflow)
        self.assertIn("base_sha='${{ github.event.pull_request.base.sha }}'", workflow)
        self.assertIn("f'{base_sha}:config/live_champion.json'", workflow)
        self.assertIn("assert live == base_live", workflow)

    def test_v6_research_smoke_collects_forward_maker_evidence(self) -> None:
        workflow = (ROOT / ".github" / "workflows" / "v6-research-smoke.yml").read_text()
        self.assertNotIn(": > v6_evidence/trade_tape.csv", workflow)
        self.assertIn("polymarket_trade_recorder", workflow)
        self.assertIn("record_trade_tape()", workflow)
        self.assertIn("maker_tick()", workflow)
        self.assertIn("for delay in 20 20 20 20", workflow)
        self.assertIn("--hold-seconds 45", workflow)
        self.assertIn("'trade_tape_rows':tape_rows", workflow)
        self.assertIn("'maker_fill_rows':fill_rows", workflow)


if __name__ == "__main__":
    unittest.main()
