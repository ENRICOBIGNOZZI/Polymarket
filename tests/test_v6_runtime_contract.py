from __future__ import annotations

import importlib.util
import json
import math
import re
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
        cls.hard_safety = load_script("v6_hard_safety_policy_test", "scripts/hard_safety_policy.py")

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

        ceilings = self.hard_safety.V6_AUTHORIZED_CEILINGS
        self.assertEqual(float(ceilings["max_drawdown"]), 0.15)
        self.assertEqual(float(ceilings["max_market_fraction"]), 0.05)
        self.assertEqual(float(ceilings["max_event_fraction"]), 0.15)
        self.assertEqual(float(ceilings["max_gross_fraction"]), 0.70)
        self.assertEqual(float(ceilings["global_max_drawdown"]), 0.15)
        self.assertEqual(float(ceilings["global_max_gross_fraction"]), 0.70)

        self.assertEqual(float(cfg["max_drawdown"]), 0.15)
        self.assertGreater(float(cfg["max_market_fraction"]), 0.0)
        self.assertLessEqual(float(cfg["max_market_fraction"]), float(ceilings["max_market_fraction"]))
        self.assertGreater(float(cfg["max_event_fraction"]), 0.0)
        self.assertLessEqual(float(cfg["max_event_fraction"]), float(ceilings["max_event_fraction"]))
        self.assertGreater(float(cfg["max_gross_fraction"]), 0.0)
        self.assertLessEqual(float(cfg["max_gross_fraction"]), float(ceilings["max_gross_fraction"]))
        self.assertIs(cfg["multi_strategy"]["paper_only"], True)
        self.assertEqual(float(cfg["multi_strategy"]["global_max_drawdown"]), 0.15)
        self.assertGreater(float(cfg["multi_strategy"]["global_max_gross_fraction"]), 0.0)
        self.assertLessEqual(
            float(cfg["multi_strategy"]["global_max_gross_fraction"]),
            float(ceilings["global_max_gross_fraction"]),
        )

        self.assertLessEqual(int(cfg["market_limit"]), 1000)
        self.assertGreaterEqual(float(cfg["min_liquidity"]), 2.0)
        self.assertGreaterEqual(float(cfg["min_net_edge"]), 0.00005)
        self.assertEqual(float(cfg["uncertainty_penalty"]), 0.0)
        self.assertGreater(float(cfg["fractional_kelly"]), 0.0)
        self.assertLessEqual(float(cfg["fractional_kelly"]), 0.25)
        self.assertGreater(float(cfg["max_trade_usd"]), 0.0)
        self.assertLessEqual(float(cfg["max_trade_usd"]), 125.0)
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

    def test_v6_runtime_term_trap_stops_children_instead_of_restarting_them(self) -> None:
        loop = (ROOT / "scripts" / "paper_v6_loop.sh").read_text(encoding="utf-8")
        self.assertIn("shutdown(){ trap - EXIT INT TERM; cleanup; exit 0; }", loop)
        self.assertIn("trap cleanup EXIT\ntrap shutdown INT TERM", loop)

    def test_v6_runtime_requires_proven_proxy_port_and_single_broker_owner(self) -> None:
        loop = (ROOT / "scripts" / "paper_v6_loop.sh").read_text(encoding="utf-8")
        launcher = (ROOT / "scripts" / "v6_multileg_launcher.py").read_text(encoding="utf-8")
        self.assertIn("reap_stale_v6_proxy_listener", loop)
        self.assertIn("stale_v6_proxy_listener_reaped=", loop)
        self.assertIn("proxy_pid_owns_port", loop)
        self.assertIn("wait_for_owned_proxy", loop)
        self.assertIn("failed to start with verified port ownership", loop)
        self.assertIn("lost verified listener ownership or health", loop)
        # Multi-leg stale-owner recovery has exactly one authority: the lock-owning
        # launcher. The outer loop must not scan/kill a second broker candidate set.
        self.assertNotIn("reap_stale_v6_brokers", loop)
        self.assertNotIn("stale_v6_broker_reaped=", loop)
        self.assertIn("_recover_stale_owner", launcher)
        self.assertIn("_safe_stale_owner", launcher)
        self.assertIn("stale_v6_multileg_owner_reaped=", launcher)

    def test_maker_fill_replay_is_late_index_safe_and_queue_aware(self) -> None:
        source = (ROOT / "src" / "maker_paper.cpp").read_text(encoding="utf-8")
        self.assertIn("{o.condition_id}, o.created_ts, tape_until, 10000", source)
        self.assertNotIn("std::max(o.created_ts, o.last_trade_ts), tape_until", source)
        self.assertIn("if (cursor_contains(o, trade.id)) continue;", source)
        self.assertIn("QUEUE_TRADE_DEPLETION", source)
        self.assertIn("QUEUE_CANCEL_DEPLETION", source)
        self.assertIn("SKIP_QUEUE", source)
        self.assertIn("candidate >= ask - 1e-12", source)
        loop = (ROOT / "scripts" / "paper_v6_loop.sh").read_text(encoding="utf-8")
        cfg = json.loads((ROOT / "config/paper_v6.json").read_text(encoding="utf-8"))
        self.assertIn("--improve-ticks 1", loop)
        self.assertIn("--max-queue-multiple 6", loop)
        self.assertIn('--min-edge "$INTENT_MIN_EDGE"', loop)
        self.assertIn('INTENT_MIN_EDGE="${V6_INTENT_MIN_EDGE:-$CONFIG_INTENT_MIN_EDGE}"', loop)
        self.assertIn('MAX_TRADE_USD="${V6_MAX_TRADE_USD:-$CONFIG_MAX_TRADE_USD}"', loop)
        self.assertIn('HARD_ARB_MAX_TRADE_USD="${V6_HARD_ARB_MAX_TRADE_USD:-$CONFIG_HARD_ARB_MAX_TRADE_USD}"', loop)
        self.assertGreaterEqual(float(cfg["v6"]["intent_min_edge"]), 0.00005)
        self.assertIn('--max-order-usd "$MAX_TRADE_USD"', loop)
        self.assertIn('--max-trade-usd "$MAX_TRADE_USD"', loop)
        self.assertIn('--max-trade-usd "$HARD_ARB_MAX_TRADE_USD"', loop)
        self.assertLessEqual(float(cfg["max_trade_usd"]), 125.0)
        self.assertLessEqual(float(cfg["v6"]["hard_arb_max_trade_usd"]), 125.0)
        numeric_order_values = [float(x) for x in re.findall(r"--max-order-usd\s+([0-9.]+)", loop)]
        numeric_trade_values = [float(x) for x in re.findall(r"--max-trade-usd\s+([0-9.]+)", loop)]
        self.assertTrue(all(0.0 < x <= 125.0 for x in numeric_order_values + numeric_trade_values))

    def test_v6_research_smoke_preserves_base_live_selector(self) -> None:
        workflow = (ROOT / ".github" / "workflows" / "v6-research-smoke.yml").read_text()
        self.assertIn("fetch-depth: 0", workflow)
        self.assertIn("base_sha='${{ github.event.pull_request.base.sha }}'", workflow)
        self.assertIn("f'{base_sha}:config/live_champion.json'", workflow)
        self.assertIn("assert live == base_live", workflow)
        self.assertNotIn("assert live['version'] == 5, 'non-integration research smoke", workflow)


if __name__ == "__main__":
    unittest.main()
