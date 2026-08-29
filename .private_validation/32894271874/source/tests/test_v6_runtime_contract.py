from __future__ import annotations

import importlib.util
import json
import math
import re
import subprocess
import sys
import tempfile
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

        # Authorized aggressive PAPER envelope. Current runtime may remain stricter
        # until its research/integration evidence is approved, but CI must not encode
        # the superseded 2.5% / 8% / 45% profile as an immutable safety contract.
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
        self.assertEqual(self.relations.threshold_signature("Will EUR/USD exceed €1.25?")[2], 1.25)
        self.assertEqual(self.relations.threshold_signature("Will GBP/USD exceed £1.40?")[2], 1.40)

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
        self.assertIn("v6_queue_filter.py micro", loop)
        self.assertIn("graph_research_ev.py", loop)
        self.assertIn("v6_queue_filter.py hard", loop)
        self.assertIn("--leg-latency-ms 100", loop)
        self.assertIn("polymarket_maker_paper", loop)

    def test_v6_runtime_term_trap_stops_children_instead_of_restarting_them(self) -> None:
        loop = (ROOT / "scripts" / "paper_v6_loop.sh").read_text(encoding="utf-8")
        self.assertIn("shutdown(){ trap - EXIT INT TERM; cleanup; exit 0; }", loop)
        self.assertIn("trap cleanup EXIT\ntrap shutdown INT TERM", loop)

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
        self.assertIn("--improve-ticks 1", loop)
        self.assertIn("--max-queue-multiple 6", loop)
        self.assertIn('--min-edge "$INTENT_MIN_EDGE"', loop)

        edge_default = re.search(r'INTENT_MIN_EDGE="\$\{V6_INTENT_MIN_EDGE:-([0-9.]+)\}"', loop)
        self.assertIsNotNone(edge_default)
        self.assertGreaterEqual(float(edge_default.group(1)), 0.00005)

        max_order_values = [float(x) for x in re.findall(r"--max-order-usd\s+([0-9.]+)", loop)]
        max_trade_values = [float(x) for x in re.findall(r"--max-trade-usd\s+([0-9.]+)", loop)]
        self.assertTrue(max_order_values)
        self.assertTrue(max_trade_values)
        self.assertTrue(all(0.0 < x <= 125.0 for x in max_order_values + max_trade_values))

    def test_graph_is_research_only_and_micro_exploration_stays_tiny(self) -> None:
        cfg = json.loads((ROOT / "config/paper_v6.json").read_text())
        graph = cfg["v6"]["graph"]
        alpha = cfg["v6"]["micro_taker_alpha"]
        exploration = cfg["v6"]["micro_taker_exploration"]
        self.assertEqual(graph["mode"], "research_only")
        self.assertIs(graph["broker_routing_enabled"], False)
        self.assertTrue(alpha["paper_only"])
        self.assertGreaterEqual(int(alpha["min_independent_blocks"]), 30)
        self.assertGreaterEqual(int(alpha["min_nonzero_blocks"]), 30)
        self.assertGreaterEqual(int(alpha["min_oos_blocks"]), 10)
        self.assertLessEqual(int(alpha["min_oos_blocks"]), 16)
        self.assertGreaterEqual(float(alpha["oos_error_quantile"]), 0.90)
        self.assertGreaterEqual(int(alpha["min_activity_trades_60s"]), 1)
        self.assertGreater(float(alpha["min_entry_price"]), 0.0)
        self.assertLess(float(alpha["min_entry_price"]), float(alpha["max_entry_price"]))
        self.assertLess(float(alpha["max_entry_price"]), 1.0)
        self.assertLessEqual(float(alpha["max_round_trip_cost_fraction"]), 0.03)
        self.assertTrue(exploration["paper_only"])
        self.assertTrue(exploration["enabled"])
        self.assertLessEqual(float(exploration["max_trade_usd"]), 5.0)
        self.assertLessEqual(int(exploration["max_opens_per_hour"]), 6)
        self.assertLessEqual(int(exploration["max_positions"]), 2)
        self.assertGreaterEqual(int(exploration["hold_seconds"]), 30)
        self.assertLessEqual(int(exploration["hold_seconds"]), 60)
        self.assertGreater(float(exploration["min_entry_price"]), 0.0)
        self.assertLess(float(exploration["min_entry_price"]), float(exploration["max_entry_price"]))
        self.assertLess(float(exploration["max_entry_price"]), 1.0)
        self.assertGreater(float(exploration["max_round_trip_cost_fraction"]), 0.0)
        self.assertLessEqual(float(exploration["max_round_trip_cost_fraction"]), 0.03)

    def test_micro_taker_child_preserves_paper_alpha_config(self) -> None:
        cfg = json.loads((ROOT / "config/paper_v6.json").read_text(encoding="utf-8"))
        with tempfile.TemporaryDirectory() as temporary:
            run_root = Path(temporary) / "run"
            subprocess.run(
                [
                    sys.executable,
                    str(ROOT / "scripts/v6_materialize_configs.py"),
                    "--config", str(ROOT / "config/paper_v6.json"),
                    "--run-root", str(run_root),
                ],
                check=True,
                capture_output=True,
                text=True,
            )
            child = json.loads((run_root / "micro_taker_config.json").read_text(encoding="utf-8"))
        self.assertTrue(child["v6"]["micro_taker_alpha"]["paper_only"])
        self.assertTrue(child["v6"]["micro_taker_exploration"]["paper_only"])
        self.assertEqual(child["v6"]["micro_taker_alpha"], cfg["v6"]["micro_taker_alpha"])
        self.assertEqual(child["v6"]["micro_taker_exploration"], cfg["v6"]["micro_taker_exploration"])
        loop = (ROOT / "scripts/paper_v6_loop.sh").read_text(encoding="utf-8")
        self.assertIn("'micro_taker_alpha':alpha", loop)
        self.assertIn("'micro_taker_exploration':exploration", loop)

    def test_v6_research_smoke_preserves_base_live_selector(self) -> None:
        workflow = (ROOT / ".github" / "workflows" / "v6-research-smoke.yml").read_text()
        self.assertIn("fetch-depth: 0", workflow)
        self.assertIn("base_sha='${{ github.event.pull_request.base.sha }}'", workflow)
        self.assertIn("f'{base_sha}:config/live_champion.json'", workflow)
        self.assertIn("assert live == base_live", workflow)
        self.assertNotIn("assert live['version'] == 5, 'non-integration research smoke", workflow)

    def test_v6_research_smoke_feeds_and_observes_tiny_exploration(self) -> None:
        workflow = (ROOT / ".github" / "workflows" / "v6-research-smoke.yml").read_text()
        self.assertIn("./build/polymarket_trade_recorder", workflow)
        self.assertIn("--interval 5 --loop", workflow)
        self.assertIn("probe_markets=250", workflow)
        self.assertEqual(workflow.count('--markets "$probe_markets"'), 2)
        self.assertNotIn("--markets 120", workflow)
        self.assertIn("stop_smoke_recorder", workflow)
        self.assertIn('kill -0 "$recorder_pid"', workflow)
        self.assertIn("--trade-tape v6_evidence/trade_tape.csv", workflow)
        self.assertIn("--exploration-hold-seconds 45", workflow)
        self.assertIn("for i in 1 2 3 4 5 6 7 8 9 10 11 12 13 14 15 16; do", workflow)
        self.assertIn("--horizon-seconds 10", workflow)
        self.assertIn("--max-target-staleness-seconds 5", workflow)
        self.assertIn("--exploration-max-opens-per-hour 3", workflow)
        self.assertIn("micro_taker_exploration_buy_fills", workflow)
        self.assertIn("github.run_attempt", workflow)
        self.assertIn("micro_taker_exploration_hourly_opens", workflow)
        self.assertIn("micro_taker_exploration_closed", workflow)
        self.assertIn("micro_taker_exploration_economic_rejections", workflow)
        self.assertIn("micro_taker_exploration_best_round_trip_cost_fraction", workflow)


if __name__ == "__main__":
    unittest.main()
