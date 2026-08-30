#!/usr/bin/env python3
from __future__ import annotations

import json
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


class V7MarketMakerCppRuntimeContractTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.source = (ROOT / "src" / "v7_market_maker_runtime.cpp").read_text(encoding="utf-8")
        cls.cmake = (ROOT / "CMakeLists.txt").read_text(encoding="utf-8")
        cls.policy = json.loads((ROOT / "config" / "v7_professional_market_maker.json").read_text(encoding="utf-8"))

    def test_cpp_runtime_is_built_from_common_v7_components(self) -> None:
        self.assertIn("polymarket_v7_market_maker_runtime", self.cmake)
        self.assertIn("pm_v7_hft", self.cmake)
        self.assertIn("pm_fast_arb", self.cmake)
        for token in (
            "MarketWebSocketFeed",
            "MarketWsShard",
            "MakerInstrumentLane",
            "MakerPaperExecutionPolicy",
            "SleeveCapitalAccount",
            "MakerModelStore",
            "SpscRing",
            "ExecutionCore",
        ):
            with self.subTest(token=token):
                self.assertIn(token, self.source)
        self.assertNotIn("MakerPaperMarketEngine paper", self.source)
        self.assertNotIn("market.paper.apply_intent", self.source)

    def test_hot_callback_has_no_rest_or_filesystem_path(self) -> None:
        start = self.source.index("void on_payload(std::string_view payload")
        end = self.source.index("void on_transport_error()", start)
        hot = self.source[start:end]
        for forbidden in (
            "fetch_books(",
            "discover_markets(",
            "read_file(",
            "read_json(",
            "atomic_write(",
            "std::ofstream",
            "std::ifstream",
            "sleep_for",
        ):
            with self.subTest(forbidden=forbidden):
                self.assertNotIn(forbidden, hot)
        self.assertIn("decoder_->process_frame", hot)
        self.assertIn("process_event", hot)

    def test_paper_runtime_never_contains_authenticated_execution_material(self) -> None:
        self.assertNotIn("PRIVATE_KEY", self.source)
        self.assertNotIn("POLYMARKET_PRIVATE_KEY", self.source)
        self.assertNotIn("create_order", self.source)
        self.assertNotIn("post_order", self.source)
        self.assertIn('"paper_only"', self.source)
        self.assertIn('"authenticated_execution"', self.source)
        self.assertIn('"real_order_submission"', self.source)

    def test_queue_assumptions_are_explicit_and_conservative(self) -> None:
        queue = self.policy["paper_queue"]
        self.assertFalse(queue["exact_fifo_claimed"])
        self.assertEqual(queue["operational_fill_scenario"], "pessimistic")
        self.assertEqual(int(queue["assumed_submission_latency_ms"]), 1)
        multipliers = queue["queue_ahead_multipliers"]
        self.assertEqual(float(multipliers["lower"]), 1.0)
        self.assertGreaterEqual(float(multipliers["expected"]), float(multipliers["lower"]))
        self.assertGreaterEqual(float(multipliers["upper"]), float(multipliers["expected"]))
        self.assertGreaterEqual(float(queue["cold_start_queue_confidence"]), 0.0)
        self.assertLessEqual(float(queue["cold_start_queue_confidence"]), 1.0)
        self.assertTrue(queue["public_print_conservation_required"])
        self.assertTrue(queue["cancel_pending_remains_fillable_until_effective"])
        self.assertFalse(queue["pre_arrival_flow_can_deplete_queue"])

    def test_unmeasured_rewards_neither_authorize_nor_inflate_quotes(self) -> None:
        self.assertIn("context.conservative_rebate_ev_per_share = 0.0;", self.source)
        self.assertIn("context.conservative_reward_ev_per_share = 0.0;", self.source)
        self.assertNotIn(
            "model.base_quote_shares = std::max(model.base_quote_shares,\n"
            "                                           market.cold.rewards_min_size);",
            self.source,
        )

    def test_runtime_loads_bounded_exploration_and_soft_inventory_policy(self) -> None:
        exploration = self.policy["exploration"]
        per_quote = float(exploration["max_quote_notional_fraction"])
        per_market = float(exploration["max_market_fraction"])
        total = float(exploration["max_capital_fraction"])
        self.assertLessEqual(2.0 * per_quote, per_market)
        expected_market_cap = int(total // (2.0 * per_quote))
        self.assertEqual(expected_market_cap, 5)
        for token in (
            'inventory, "soft_directional_inventory_fraction"',
            'exploration, "max_quote_notional_fraction"',
            'exploration, "max_market_fraction"',
            'exploration, "max_capital_fraction"',
            "exploration_market_cap",
            "model.exploration_max_active_markets = configured_market_capacity",
            "model.exploration_concurrent_market_cap = exploration_market_cap",
            "context.risk.exploration_max_quote_shares",
        ):
            with self.subTest(token=token):
                self.assertIn(token, self.source)

    def test_spool_remains_transport_and_python_stays_canonical_writer(self) -> None:
        self.assertIn('run_root_ / "ledger" / "spool"', self.source)
        self.assertNotIn('run_root_ / "ledger" / "execution.jsonl"', self.source)
        self.assertIn("v7_ledger_spool", self.policy["architecture"]["canonical_ledger_transport"])
        self.assertTrue(self.policy["architecture"]["single_canonical_ledger_writer"])

    def test_feed_errors_invalidate_lineage_and_do_not_silently_continue(self) -> None:
        self.assertIn("invalidate_all_decisions(receive_now())", self.source)
        self.assertIn("global_kill", self.source)
        self.assertIn("maker_cpp_order_tx_or_telemetry_invariant_failure", self.source)

    def test_cold_control_plane_freezes_new_maker_risk_without_filesystem_on_hot_path(self) -> None:
        self.assertIn('"MAKER_FREEZE"', self.source)
        self.assertIn("new_risk_frozen.store", self.source)
        self.assertIn("context.risk.new_risk_frozen", self.source)

    def test_execution_core_owns_paper_state_and_prioritizes_control(self) -> None:
        self.assertIn("class ExecutionCore final", self.source)
        self.assertIn("MakerPaperExecutionPolicy policy_{}", self.source)
        self.assertIn("SleeveCapitalAccount capital_{}", self.source)
        self.assertIn("std::thread execution_thread", self.source)
        self.assertIn("pop_critical(command)", self.source)
        self.assertIn("pop_normal(command)", self.source)
        self.assertLess(
            self.source.index("pop_critical(command)"),
            self.source.index("pop_normal(command)"),
        )
        # A priority cancel can overtake an older placement. The execution owner
        # must then suppress that stale quote instead of resurrecting it.
        self.assertIn("record_control_watermark(intent", self.source)
        self.assertIn("quote_is_superseded(intent)", self.source)
        self.assertIn("economic_control_preempts_fresh_quote(command)", self.source)
        self.assertIn("minimum_quote_lifetime_ns_", self.source)
        self.assertIn("exploration_concurrent_market_cap_", self.source)
        self.assertIn("refresh_exploration_market", self.source)
        self.assertLess(
            self.source.index("quote_is_superseded(intent)"),
            self.source.index("policy_.process(command.plan, capital_)"),
        )


if __name__ == "__main__":
    unittest.main()
