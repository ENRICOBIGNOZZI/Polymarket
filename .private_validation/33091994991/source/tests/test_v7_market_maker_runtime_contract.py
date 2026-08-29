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
        self.assertGreater(v7["micro_maker_capital_fraction"], v7["relative_value_capital_fraction"])
        self.assertGreaterEqual(v7["micro_maker_capital_fraction"], 0.50)
        self.assertEqual(v7["micro_maker_policy"], "config/v7_professional_market_maker.json")
        self.assertAlmostEqual(
            sum(float(v7[key]) for key in (
                "micro_maker_capital_fraction", "micro_taker_capital_fraction",
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
        self.assertTrue(policy["exploration"]["enabled"])
        self.assertFalse(policy["exploration"]["promotion_credit"])
        self.assertLessEqual(float(policy["exploration"]["max_capital_fraction"]), 0.02)
        self.assertTrue(policy["execution_model"]["product_of_marginals_forbidden"])
        self.assertTrue(policy["capital"]["queue_never_grants_size"])

    def test_canonical_runtime_starts_maker_instead_of_old_disabled_status(self) -> None:
        source = (ROOT / "scripts" / "paper_v7_execution_loop.sh").read_text(encoding="utf-8")
        self.assertIn("v7_market_maker_worker.py", source)
        self.assertIn("v7_market_maker_rewards.py", source)
        self.assertIn("v7_market_maker_model.py", source)
        self.assertIn("v7_market_maker_status.py", source)
        self.assertIn("v7_ledger_spool.py", source)
        self.assertNotIn("direct_joint_fill_conditioned_ev_not_yet_mature_generic_maker_rejected", source)
        self.assertNotIn("Generic maker is intentionally not started", source)

    def test_fast_path_contract_forbids_rest_on_quote_hot_path(self) -> None:
        policy = json.loads((ROOT / "config" / "v7_professional_market_maker.json").read_text(encoding="utf-8"))
        self.assertTrue(policy["latency"]["event_driven"])
        self.assertTrue(policy["latency"]["rest_polling_not_allowed_on_quote_fast_path"])
        self.assertLessEqual(int(policy["latency"]["target_decision_p99_us"]), 1500)
        self.assertLessEqual(int(policy["latency"]["target_cancel_decision_p99_us"]), 1000)

    def test_master_v7_authority_preserves_one_professional_maker(self) -> None:
        directives = json.loads((ROOT / "config" / "operator_directives.json").read_text(encoding="utf-8"))
        self.assertEqual(directives["operator_instruction_id"], "user-v7-master-multi-agent-operating-prompt-20260827")
        self.assertEqual(directives["priority_instruction_id"], "user-v7-professional-market-making-priority-20260827")
        self.assertFalse(directives["paper_v7_authorization"]["authenticated_execution"])
        architecture = directives["architecture"]
        self.assertTrue(architecture["single_runtime_owner"])
        self.assertTrue(architecture["single_execution_ledger"])
        self.assertTrue(architecture["professional_market_maker_is_v7_sleeve_not_new_runtime"])
        self.assertIn("professional_market_maker", directives["model_contracts"]["micro_maker"])
        forbidden = "\n".join(directives["forbidden_regressions"])
        self.assertIn("Do not add authenticated or real-money execution", forbidden)
        self.assertIn("Do not create a second maker runtime", forbidden)


if __name__ == "__main__":
    unittest.main()
