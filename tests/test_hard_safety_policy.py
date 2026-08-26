from __future__ import annotations

import copy
import unittest

from scripts.hard_safety_policy import (
    compare_paper_config,
    compare_runtime_hard_safety,
    is_runtime_hard_safety_surface,
)


def v7_envelope() -> dict:
    return {
        "schema_version": 2,
        "engine_version": 7,
        "paper_only": True,
        "market_limit": 1000,
        "min_liquidity": 2.0,
        "min_net_edge": 0.00005,
        "uncertainty_penalty": 0.0,
        "fractional_kelly": 0.25,
        "fixed_dollar_trade_cap_enabled": False,
        "max_trade_usd": 1e100,
        "max_trade_fraction": 1.0,
        "max_market_fraction": 1.0,
        "max_event_fraction": 1.0,
        "max_gross_fraction": 1.0,
        "max_drawdown": 0.15,
        "multi_strategy": {
            "paper_only": True,
            "global_max_drawdown": 0.15,
            "global_max_gross_fraction": 1.0,
        },
        "v7": {
            "paper_only": True,
            "micro_maker_capital_fraction": 0.22,
            "micro_taker_capital_fraction": 0.12,
            "relative_value_capital_fraction": 0.34,
            "hard_arb_capital_fraction": 0.22,
            "external_capital_fraction": 0.08,
            "reserve_fraction": 0.02,
            "intent_min_edge": 0.00005,
            "hard_arb_min_net_edge": 0.00005,
            "hard_arb_fixed_dollar_trade_cap_enabled": False,
            "hard_arb_max_trade_usd": 1e100,
            "hard_arb_max_trade_fraction": 1.0,
            "authoritative_fee_required": True,
            "shared_execution_ledger_required": True,
            "joint_fill_state_required_for_multileg": True,
            "authenticated_execution": False,
        },
    }


class HardSafetyPolicyTest(unittest.TestCase):
    def test_authorized_v7_envelope_is_allowed(self) -> None:
        current = v7_envelope()
        self.assertEqual(compare_paper_config({}, current, "config/paper_v7.json"), [])

    def test_retired_versioned_config_is_rejected(self) -> None:
        for path in ("config/paper_v3.json", "config/paper_v4.json", "config/paper_v5.json", "config/paper_v6.json"):
            errors = compare_paper_config({}, {}, path)
            self.assertEqual(errors, [f"retired/noncanonical paper configuration is not supported: {path}"])

    def test_v7_bounded_policy_rollback_is_rejected(self) -> None:
        current = v7_envelope()
        current["fixed_dollar_trade_cap_enabled"] = True
        current["max_trade_usd"] = 125.0
        current["max_market_fraction"] = 0.05
        current["max_event_fraction"] = 0.15
        current["max_gross_fraction"] = 0.70
        current["multi_strategy"]["global_max_gross_fraction"] = 0.70
        current["v7"]["hard_arb_fixed_dollar_trade_cap_enabled"] = True
        current["v7"]["hard_arb_max_trade_usd"] = 125.0
        joined = "\n".join(compare_paper_config({}, current, "config/paper_v7.json"))
        self.assertIn("max_market_fraction required=1", joined)
        self.assertIn("max_event_fraction required=1", joined)
        self.assertIn("max_gross_fraction required=1", joined)
        self.assertIn("fixed_dollar_trade_cap_enabled must be false", joined)
        self.assertIn("hard_arb_fixed_dollar_trade_cap_enabled must be false", joined)

    def test_v7_economic_and_execution_safety_still_bind(self) -> None:
        current = v7_envelope()
        current["market_limit"] = 1001
        current["min_liquidity"] = 1.99
        current["min_net_edge"] = 0.0
        current["uncertainty_penalty"] = -0.01
        current["fractional_kelly"] = 0.251
        current["max_drawdown"] = 0.151
        current["v7"]["authenticated_execution"] = True
        current["v7"]["authoritative_fee_required"] = False
        current["v7"]["shared_execution_ledger_required"] = False
        current["v7"]["joint_fill_state_required_for_multileg"] = False
        joined = "\n".join(compare_paper_config({}, current, "config/paper_v7.json"))
        self.assertIn("market_limit allowed<=1000, got 1001", joined)
        self.assertIn("min_liquidity required>=2, got 1.99", joined)
        self.assertIn("min_net_edge required>=5e-05, got 0", joined)
        self.assertIn("uncertainty_penalty required>=0, got -0.01", joined)
        self.assertIn("fractional_kelly allowed<=0.25, got 0.251", joined)
        self.assertIn("max_drawdown allowed<=0.15, got 0.151", joined)
        self.assertIn("authenticated_execution must remain false", joined)
        self.assertIn("authoritative_fee_required must remain true", joined)
        self.assertIn("shared_execution_ledger_required must remain true", joined)
        self.assertIn("joint_fill_state_required_for_multileg must remain true", joined)

    def test_v7_no_cap_uses_nonbinding_dollar_sentinels(self) -> None:
        current = v7_envelope()
        current["max_trade_usd"] = 125.0
        current["v7"]["hard_arb_max_trade_usd"] = 125.0
        joined = "\n".join(compare_paper_config({}, current, "config/paper_v7.json"))
        self.assertIn("max_trade_usd must be a nonbinding compatibility sentinel", joined)
        self.assertIn("hard_arb_max_trade_usd must be a nonbinding compatibility sentinel", joined)

    def test_v7_capital_allocations_must_sum_to_one(self) -> None:
        current = v7_envelope()
        current["v7"]["reserve_fraction"] = 0.03
        joined = "\n".join(compare_paper_config({}, current, "config/paper_v7.json"))
        self.assertIn("capital allocations must sum to 100%", joined)

    def test_retired_v6_namespace_is_rejected_inside_v7(self) -> None:
        current = v7_envelope()
        current["v6"] = {"compatibility_only": True}
        joined = "\n".join(compare_paper_config({}, current, "config/paper_v7.json"))
        self.assertIn("retired compatibility namespace present", joined)

    def test_runtime_hard_safety_surfaces_are_v7_only(self) -> None:
        self.assertTrue(is_runtime_hard_safety_surface("scripts/paper_v7_loop.sh"))
        self.assertTrue(is_runtime_hard_safety_surface("scripts/paper_v7_execution_loop.sh"))
        self.assertFalse(is_runtime_hard_safety_surface("scripts/paper_v6_loop.sh"))
        self.assertFalse(is_runtime_hard_safety_surface("scripts/v6_materialize_configs.py"))

    def test_new_runtime_hard_safety_override_is_rejected(self) -> None:
        base = "child={k:x for k,x in cfg.items()}\n"
        current = base + "MAX_MARKET_FRACTION=0.06\n"
        errors = compare_runtime_hard_safety(base, current, "scripts/paper_v7_execution_loop.sh")
        self.assertEqual(len(errors), 1)
        self.assertIn("MAX_MARKET_FRACTION", errors[0])

    def test_runtime_admission_changes_without_hard_safety_write_are_allowed(self) -> None:
        base = "run_maker --min-edge 0.00035 --max-order-usd 25\n"
        current = "run_maker --min-edge 0.00005 --max-order-usd 125 --improve-ticks 1\n"
        self.assertEqual(compare_runtime_hard_safety(base, current, "scripts/paper_v7_execution_loop.sh"), [])


if __name__ == "__main__":
    unittest.main()
