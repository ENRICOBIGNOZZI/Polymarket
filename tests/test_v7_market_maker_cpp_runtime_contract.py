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

    def test_selector_token_action_side_authority_is_fail_closed_and_audited(self) -> None:
        for token in (
            "execution_cell_authority_required",
            "execution_authority_semantics",
            "authorized_execution_cells",
            "yes_execution_authority_mask",
            "no_execution_authority_mask",
            "context.selector_execution_authority_mask",
            'metadata["selector_execution_authority"]',
            '"chosen_cell_authorized"',
            '"inventory_reduction_bypass"',
            "inventory_seed_authorized",
            "sell_inventory_authorized",
            "inventory_drain_active_by_market_",
        ):
            with self.subTest(token=token):
                self.assertIn(token, self.source)

    def test_selector_retirement_preserves_directional_inventory_for_passive_exit(self) -> None:
        start = self.source.index("[[nodiscard]] bool sync_inventory_drain_mode()")
        end = self.source.index("[[nodiscard]] bool maybe_replenish", start)
        control = self.source[start:end]
        self.assertIn("!globally_requested && authority_retired", control)
        self.assertIn("inventory->directional_microunits != 0", control)
        self.assertIn("inventory_targets_[handle] = 0", control)
        self.assertIn("directional_rotation_preservations_", control)
        self.assertNotIn("liquidate_directional_inventory", control)

    def test_runtime_loads_bounded_exploration_and_soft_inventory_policy(self) -> None:
        exploration = self.policy["exploration"]
        per_quote = float(exploration["max_quote_notional_fraction"])
        per_market = float(exploration["max_market_fraction"])
        total = float(exploration["max_capital_fraction"])
        self.assertLessEqual(2.0 * per_quote, per_market)
        expected_execution_cap = int(total // (2.0 * per_quote))
        self.assertEqual(expected_execution_cap, 5)
        self.assertGreater(float(exploration["action_information_fraction"]), 0.0)
        self.assertLessEqual(float(exploration["action_information_fraction"]), 0.25)
        selection = self.policy["market_selection"]
        recent = selection["recent_flow"]
        self.assertEqual(
            int(selection["cold_start_maximum_markets"]),
            int(selection["max_active_markets"]),
        )
        self.assertEqual(
            int(recent["minimum_operational_markets"]), expected_execution_cap
        )
        self.assertEqual(
            int(recent["observation_universe_markets"]),
            int(selection["max_active_markets"]),
        )
        self.assertEqual(
            int(recent["maximum_zero_flow_reserve_markets"]),
            int(selection["max_active_markets"]),
        )
        for token in (
            'inventory, "soft_directional_inventory_fraction"',
            'exploration, "max_quote_notional_fraction"',
            'exploration, "max_market_fraction"',
            'exploration, "max_capital_fraction"',
            'exploration, "action_information_fraction"',
            "exploration_market_cap",
            "model.exploration_max_active_markets = configured_market_capacity",
            "model.exploration_concurrent_market_cap = exploration_market_cap",
            "model.exploration_max_market_fraction = exploration_market_fraction",
            "context.risk.exploration_max_quote_shares",
            "minimum_order_notional <= market_notional_cap",
            "shard->set_sleeve_starting_capital(config.starting_capital)",
        ):
            with self.subTest(token=token):
                self.assertIn(token, self.source)
        for token in (
            'metadata["exploration_action_arm"]',
            'metadata["exploration_action_propensity"]',
            'metadata["exploration_selection_propensity"]',
            'metadata["exploration_assignment_propensity"]',
        ):
            with self.subTest(propensity_token=token):
                self.assertIn(token, self.source)
        self.assertEqual(float(self.policy["inventory"]["seed_min_quote_multiples"]), 1.0)
        self.assertEqual(
            float(self.policy["inventory"]["seed_max_market_fraction"]), per_market
        )
        for token in (
            'inventory, "seed_max_market_fraction"',
            "target > inventory_seed_cap_microdollars_",
            'root["inventory_seed_budget_rejections"]',
        ):
            with self.subTest(token=token):
                self.assertIn(token, self.source)

    def test_declared_inventory_factory_has_runtime_callsite_and_canonical_evidence(self) -> None:
        inventory = self.policy["inventory"]
        self.assertTrue(inventory["auto_split_merge_accounting"])
        self.assertTrue(inventory["inventory_factory_enabled"])
        self.assertIn("policy_.split_complete_sets(", self.source)
        self.assertIn("policy_.set_complete_set_reserve_floor", self.source)
        self.assertIn('write_inventory_event(record, "INVENTORY_SPLIT")', self.source)
        self.assertIn('write_inventory_event(record, "INVENTORY_MERGE")', self.source)
        self.assertIn("balanced_complete_set_microunits", self.source)
        self.assertIn("yes_reserved_sell_microunits", self.source)

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
        self.assertIn("reason != Reason::QuoteLifetimeHold", self.source)
        self.assertIn("minimum_quote_lifetime_ns_", self.source)
        self.assertIn("exploration_concurrent_market_cap_", self.source)
        self.assertIn("refresh_exploration_market", self.source)
        self.assertLess(
            self.source.index("quote_is_superseded(intent)"),
            self.source.index("policy_.process(command.plan, capital_)"),
        )

    def test_order_state_telemetry_preserves_cancel_causality(self) -> None:
        self.assertIn('metadata["decision_reason"] = decision_reason_name', self.source)
        self.assertIn('metadata["decision_reason_code"]', self.source)
        self.assertIn('metadata["safety_preemption"] = safety_preemption', self.source)

    def test_runtime_aggregates_decision_and_feed_diagnostics_off_hot_path(self) -> None:
        self.assertIn("polymarket_v7_maker_runtime_diagnostics_v1", self.source)
        self.assertIn("runtime_diagnostics.json", self.source)
        self.assertIn("rejected_nonpositive_robust_ev", self.source)
        self.assertIn("rejected_positive_point_ev", self.source)
        self.assertIn("best_rejected_robust_ev_per_share", self.source)
        self.assertIn("reason_counts", self.source)
        self.assertIn("feed_connected_workers", self.source)

    def test_settlement_fair_authority_is_exact_contract_bounded_and_cold_plane_only(self) -> None:
        external = self.policy["settlement_aware_external_fair"]
        self.assertTrue(external["implemented"])
        self.assertTrue(external["enabled_for_paper_quotes"])
        self.assertTrue(external["require_model_mature"])
        self.assertTrue(external["require_positive_2x_cost_stress"])
        self.assertFalse(external["automatic_promotion"])
        self.assertFalse(external["real_money_authority"])
        for token in (
            "refresh_external_maker_fair",
            'market->cold.market_id != market_id',
            'contract, "verified"',
            'model, "mature"',
            '"virtual_2x_cost_stress_pnl"',
            "upper - lower > policy.maximum_interval_width",
            "context.related_fair_lower",
            "context.related_fair_upper",
        ):
            with self.subTest(token=token):
                self.assertIn(token, self.source)
        start = self.source.index("void on_payload(std::string_view payload")
        end = self.source.index("void on_transport_error()", start)
        self.assertNotIn("refresh_external_maker_fair", self.source[start:end])


if __name__ == "__main__":
    unittest.main()
