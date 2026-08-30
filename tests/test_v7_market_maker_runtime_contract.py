#!/usr/bin/env python3
from __future__ import annotations

import json
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


class ProfessionalMakerRuntimeContractTests(unittest.TestCase):
    def test_maker_is_primary_paper_sleeve_and_not_disabled(self) -> None:
        cfg = json.loads((ROOT / "config" / "paper_v7.json").read_text(encoding="utf-8"))
        v7 = cfg["v7"]
        self.assertTrue(cfg["paper_only"])
        self.assertFalse(v7["authenticated_execution"])
        self.assertFalse(v7["real_order_submission"])
        target = float(v7["execution_strategy_budget_usd"])
        self.assertAlmostEqual(
            float(cfg["starting_capital"]) * float(v7["micro_maker_capital_fraction"]),
            target,
        )
        self.assertAlmostEqual(
            v7["micro_maker_capital_fraction"], v7["relative_value_capital_fraction"]
        )
        self.assertAlmostEqual(
            float(cfg["starting_capital"]) * float(v7["external_capital_fraction"]),
            2.0 * target,
        )
        self.assertEqual(v7["micro_maker_policy"], "config/v7_professional_market_maker.json")
        self.assertAlmostEqual(
            sum(float(v7[key]) for key in (
                "micro_maker_capital_fraction", "micro_taker_capital_fraction",
                "fast_structural_capital_fraction",
                "relative_value_capital_fraction", "hard_arb_capital_fraction",
                "external_capital_fraction", "reserve_fraction",
            )),
            1.0,
        )

    def test_policy_requires_single_v7_architecture_and_bounded_exploration(self) -> None:
        policy = json.loads((ROOT / "config" / "v7_professional_market_maker.json").read_text(encoding="utf-8"))
        self.assertTrue(policy["paper_only"])
        self.assertFalse(policy["authenticated_execution"])
        self.assertFalse(policy["real_order_submission"])
        architecture = policy["architecture"]
        self.assertTrue(architecture["single_runtime_owner"])
        self.assertTrue(architecture["single_account_allocator"])
        self.assertTrue(architecture["single_canonical_ledger_writer"])
        self.assertEqual(architecture["fast_path"], "cpp_websocket_event_driven")
        self.assertEqual(architecture["slow_path"], "python_reward_selection_and_model_fit")
        self.assertTrue(policy["exploration"]["enabled"])
        self.assertFalse(policy["exploration"]["promotion_credit"])
        self.assertLessEqual(float(policy["exploration"]["max_capital_fraction"]), 0.02)
        self.assertTrue(policy["execution_model"]["product_of_marginals_forbidden"])
        self.assertTrue(policy["capital"]["queue_never_grants_size"])

    def test_canonical_runtime_starts_only_professional_maker_stack(self) -> None:
        source = (ROOT / "scripts" / "paper_v7_execution_loop.sh").read_text(encoding="utf-8")
        self.assertIn("polymarket_v7_market_maker_runtime", source)
        self.assertIn("PM_V7_MARKET_MAKER_RUNTIME", source)
        self.assertIn("polymarket_v7_maker_markout_observer", source)
        self.assertIn("PM_V7_MAKER_MARKOUT_OBSERVER", source)
        self.assertIn("v7_market_maker_rewards.py", source)
        self.assertIn('--allocation "$ALLOC/micro_maker.json"', source)
        self.assertIn('--candidate-output "$RUN_ROOT/micro_maker/reward_selection_candidate.json"', source)
        self.assertIn("--pin-runtime-selection", source)
        self.assertIn('--lookback-seconds "$MAKER_FLOW_LOOKBACK_SECONDS"', source)
        self.assertIn("v7_market_maker_model.py", source)
        self.assertIn("v7_market_maker_status.py", source)
        self.assertIn("v7_ledger_spool.py", source)
        self.assertIn("--loop --interval 0.1", source)
        self.assertIn('export PM_V7_MODEL_SHA="$SHA"', source)
        self.assertIn('MAKER_CHAMPION_MODEL="$RUN_ROOT/micro_maker/execution_model.json"', source)
        self.assertIn('MAKER_CHALLENGER_MODEL="$RUN_ROOT/micro_maker/execution_model_challenger.json"', source)
        self.assertIn('export PM_V7_MAKER_EXECUTION_MODEL="$MAKER_CHAMPION_MODEL"', source)
        self.assertIn('--artifact-role challenger', source)
        self.assertIn('--output "$MAKER_CHALLENGER_MODEL"', source)
        self.assertIn('--model "$MAKER_CHAMPION_MODEL"', source)
        self.assertNotIn('--output "$MAKER_CHAMPION_MODEL"', source)
        self.assertNotIn("v7_market_maker_worker.py", source)
        self.assertNotIn("--interval-ms 500", source)
        self.assertNotIn("v7_complete_set_maker", source)
        self.assertNotIn("polymarket_rewards_scan", source)
        self.assertNotIn("Generic maker is intentionally not started", source)

    def test_fast_structural_does_not_self_terminate_on_a_timer(self) -> None:
        options = (ROOT / "src" / "fast_runtime" / "part1.inc").read_text(encoding="utf-8")
        self.assertIn("int recycle_seconds = 0;", options)
        self.assertNotIn("int recycle_seconds = 900;", options)

    def test_runtime_routes_all_paper_execution_through_single_order_tx_owner(self) -> None:
        runtime = (ROOT / "src" / "v7_market_maker_runtime.cpp").read_text(encoding="utf-8")
        for required in (
            '"pm/v7_maker_execution_policy.hpp"',
            '"pm/v7_spsc.hpp"',
            "class ExecutionCore final",
            "MakerPaperExecutionPolicy policy_{}",
            "SleeveCapitalAccount capital_{}",
            "std::thread execution_thread",
            "execution_plan_is_critical",
            "pop_critical",
            "pop_normal",
            "push_execution_snapshot",
            "ExecutionCommandKind::PublicTrade",
            "ExecutionCommandKind::AdvanceTime",
            "policy_.process(command.plan, capital_)",
            "policy_.on_public_trade",
            "policy_.advance_time",
        ):
            self.assertIn(required, runtime)
        for forbidden in (
            "market.paper.apply_intent",
            "market.paper.on_public_trade",
            "market.paper.advance_time",
            "MakerPaperMarketEngine paper;",
        ):
            self.assertNotIn(forbidden, runtime)
        critical_pos = runtime.index("pop_critical(command)")
        normal_pos = runtime.index("pop_normal(command)")
        self.assertLess(critical_pos, normal_pos)

    def test_maker_ledger_ids_are_globally_unique_across_market_engines(self) -> None:
        runtime = (ROOT / "src" / "v7_market_maker_runtime.cpp").read_text(encoding="utf-8")
        self.assertIn('return "mmo-" + std::to_string(market) + "-" + telemetry_epoch_', runtime)
        self.assertIn('event["record_id"] = "cpp-mm-" + telemetry_epoch_', runtime)
        self.assertIn('"mmf-" + std::to_string(record.market_handle) + "-"', runtime)
        self.assertIn('telemetry_epoch_ + "-" + std::to_string(paper.order_id)', runtime)
        self.assertNotIn("struct OrderMeta", runtime)
        self.assertNotIn("orders_[paper.order_id]", runtime)

    def test_execution_cells_are_exact_sha_slow_path_and_hot_path_bounded(self) -> None:
        loader = (ROOT / "src" / "v7_maker_execution_cells.cpp").read_text(encoding="utf-8")
        kernel = (ROOT / "src" / "v7_maker_hft.cpp").read_text(encoding="utf-8")
        header = (ROOT / "include" / "pm" / "v7_maker_hft.hpp").read_text(encoding="utf-8")
        model = (ROOT / "scripts" / "v7_market_maker_model.py").read_text(encoding="utf-8")
        self.assertIn("PM_V7_MODEL_SHA", loader)
        self.assertIn("PM_V7_MAKER_EXECUTION_MODEL", loader)
        self.assertIn("kExecutionCellCount", header)
        self.assertIn("execution_cell_index", kernel)
        self.assertIn("safe_logit(cell->fill_probability)", kernel)
        self.assertIn("cell->adverse_markout_per_share", kernel)
        self.assertIn("cell->fill_weight", kernel)
        self.assertIn("cell->markout_weight", kernel)
        self.assertIn("correlated_horizons_are_not_pooled", model)
        self.assertNotIn("filesystem", kernel)
        self.assertNotIn("boost/json", kernel)

    def test_markout_observer_is_evidence_only_and_full_depth(self) -> None:
        observer = (ROOT / "src" / "v7_maker_markout_observer.cpp").read_text(encoding="utf-8")
        markout = (ROOT / "src" / "v7_markout.cpp").read_text(encoding="utf-8")
        self.assertIn('event["event_type"] = "MARKOUT"', observer)
        self.assertIn("kHorizonSeconds{{1, 10, 45, 60, 300}}", observer)
        self.assertIn("full_l10_depth", observer)
        self.assertIn("fill_conditioned", observer)
        self.assertIn("executable_markout", observer)
        self.assertIn("remaining > 0", markout)
        self.assertNotIn("v7_maker_paper.hpp", observer)
        self.assertNotIn("apply_intent", observer)
        self.assertNotIn("ORDER_SUBMITTED", observer)
        self.assertNotIn("real_order_submission", observer)

    def test_fast_path_contract_forbids_rest_on_quote_hot_path(self) -> None:
        policy = json.loads((ROOT / "config" / "v7_professional_market_maker.json").read_text(encoding="utf-8"))
        latency = policy["latency"]
        self.assertTrue(latency["event_driven"])
        self.assertTrue(latency["rest_polling_not_allowed_on_quote_fast_path"])
        acceptance = latency["acceptance_receive_to_intent_us"]
        stretch = latency["stretch_receive_to_intent_us"]
        self.assertLessEqual(int(acceptance["p99"]), 1500)
        self.assertLessEqual(int(latency["acceptance_toxicity_to_cancel_intent_p99_us"]), 1000)
        self.assertLessEqual(int(stretch["p99"]), 500)
        self.assertLessEqual(int(latency["stretch_toxicity_to_cancel_intent_p99_us"]), 250)
        self.assertTrue(latency["representative_replay_required_for_claim"])
        self.assertTrue(latency["synthetic_empty_market_benchmark_not_sufficient"])

    def test_current_operator_authority_is_v7_only_cleanup(self) -> None:
        directives = json.loads((ROOT / "config" / "operator_directives.json").read_text(encoding="utf-8"))
        self.assertEqual(
            directives["operator_instruction_id"],
            "user-retire-obsolete-generations-20260829",
        )
        self.assertEqual(
            directives["priority_instruction_id"],
            "user-retire-obsolete-generations-20260829",
        )
        self.assertTrue(directives["paper_v7_authorization"]["paper_only"])
        self.assertFalse(directives["paper_v7_authorization"]["authenticated_execution"])
        self.assertFalse(directives["paper_v7_authorization"]["real_order_submission"])
        architecture = directives["architecture"]
        self.assertTrue(architecture["single_runtime_owner"])
        self.assertTrue(architecture["single_execution_ledger"])
        self.assertTrue(architecture["professional_market_maker_is_v7_sleeve_not_new_runtime"])
        self.assertEqual(architecture["cleanup_sequence"], "audit_then_port_then_test_then_validate_then_retire_obsolete_generations")
        self.assertIn("Git history is the archive", architecture["retired_generation_rule"])
        forbidden = "\n".join(directives["forbidden_regressions"])
        self.assertIn("Do not add authenticated or real-money execution", forbidden)
        self.assertIn("Do not add or restore V3/V4/V5/V6", forbidden)


if __name__ == "__main__":
    unittest.main()
