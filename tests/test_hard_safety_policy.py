from __future__ import annotations

import copy
import unittest

from scripts.hard_safety_policy import (
    compare_paper_config,
    compare_runtime_hard_safety,
    is_runtime_hard_safety_surface,
)


BASE = {
    "market_limit": 500,
    "min_liquidity": 25.0,
    "min_net_edge": 0.00025,
    "uncertainty_penalty": 0.01,
    "fractional_kelly": 0.12,
    "max_trade_usd": 60.0,
    "pca_min_history": 24,
    "max_market_fraction": 0.025,
    "max_event_fraction": 0.08,
    "max_gross_fraction": 0.45,
    "max_drawdown": 0.15,
    "multi_strategy": {
        "paper_only": True,
        "global_max_drawdown": 0.15,
        "global_max_gross_fraction": 0.45,
        "strategies": [
            {
                "name": "micro",
                "overrides": {
                    "min_net_edge": 0.00005,
                    "max_gross_fraction": 0.40,
                    "max_drawdown": 0.15,
                },
            },
            {
                "name": "pca",
                "overrides": {
                    "pca_min_history": 24,
                    "max_gross_fraction": 0.45,
                    "max_drawdown": 0.15,
                },
            },
        ],
    },
}


def v6_envelope() -> dict:
    current = copy.deepcopy(BASE)
    current.update(
        {
            "market_limit": 1000,
            "min_liquidity": 2.0,
            "min_net_edge": 0.00005,
            "uncertainty_penalty": 0.0,
            "fractional_kelly": 0.25,
            "max_trade_usd": 125.0,
            "max_market_fraction": 0.05,
            "max_event_fraction": 0.15,
            "max_gross_fraction": 0.70,
        }
    )
    current["multi_strategy"]["global_max_gross_fraction"] = 0.70
    current["multi_strategy"]["strategies"][0]["overrides"]["max_gross_fraction"] = 0.70
    current["multi_strategy"]["strategies"][1]["overrides"]["max_gross_fraction"] = 0.70
    current["v6"] = {
        "paper_only": True,
        "micro_maker_capital_fraction": 0.22,
        "micro_taker_capital_fraction": 0.12,
        "relative_value_capital_fraction": 0.34,
        "hard_arb_capital_fraction": 0.22,
        "external_capital_fraction": 0.08,
        "reserve_fraction": 0.02,
        "intent_min_edge": 0.00005,
        "hard_arb_min_net_edge": 0.00005,
        "hard_arb_max_trade_usd": 125.0,
    }
    return current


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
            "strategies": [],
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
    def test_v5_incumbent_contract_is_preserved(self) -> None:
        current = copy.deepcopy(BASE)
        current["max_market_fraction"] = 0.05
        current["max_event_fraction"] = 0.15
        current["max_gross_fraction"] = 0.75
        current["multi_strategy"]["global_max_gross_fraction"] = 0.75
        joined = "\n".join(compare_paper_config(BASE, current, "config/paper_v5.json"))
        self.assertIn("allowed<=0.025, got 0.05", joined)
        self.assertIn("allowed<=0.08, got 0.15", joined)
        self.assertIn("allowed<=0.45, got 0.75", joined)

    def test_v6_authorized_aggressive_envelope_is_allowed(self) -> None:
        self.assertEqual(compare_paper_config(BASE, v6_envelope(), "config/paper_v6.json"), [])

    def test_v6_old_2_5_8_45_caps_are_not_immutable(self) -> None:
        current = v6_envelope()
        del current["multi_strategy"]["strategies"][0]["overrides"]["max_gross_fraction"]
        self.assertEqual(compare_paper_config(BASE, current, "config/paper_v6.json"), [])

    def test_v6_cannot_exceed_authorized_market_universe(self) -> None:
        current = v6_envelope()
        current["market_limit"] = 1001
        joined = "\n".join(compare_paper_config(BASE, current, "config/paper_v6.json"))
        self.assertIn("market_limit allowed<=1000, got 1001", joined)

        current = v6_envelope()
        current["market_limit"] = 700
        self.assertEqual(compare_paper_config(BASE, current, "config/paper_v6.json"), [])

    def test_v6_cannot_exceed_authorized_concentration_or_gross(self) -> None:
        current = v6_envelope()
        current["max_market_fraction"] = 0.051
        current["max_event_fraction"] = 0.151
        current["max_gross_fraction"] = 0.701
        current["multi_strategy"]["global_max_gross_fraction"] = 0.701
        joined = "\n".join(compare_paper_config(BASE, current, "config/paper_v6.json"))
        self.assertIn("max_market_fraction allowed<=0.05, got 0.051", joined)
        self.assertIn("max_event_fraction allowed<=0.15, got 0.151", joined)
        self.assertIn("max_gross_fraction allowed<=0.7, got 0.701", joined)
        self.assertIn("global_max_gross_fraction allowed<=0.7, got 0.701", joined)

    def test_v6_post_cost_edge_liquidity_and_uncertainty_floors_are_enforced(self) -> None:
        current = v6_envelope()
        current["min_net_edge"] = 0.0
        current["min_liquidity"] = 1.99
        current["uncertainty_penalty"] = -0.01
        current["v6"]["intent_min_edge"] = 0.0
        current["v6"]["hard_arb_min_net_edge"] = 0.000049
        joined = "\n".join(compare_paper_config(BASE, current, "config/paper_v6.json"))
        self.assertIn("min_net_edge required>=5e-05, got 0", joined)
        self.assertIn("min_liquidity required>=2, got 1.99", joined)
        self.assertIn("uncertainty_penalty required>=0, got -0.01", joined)
        self.assertIn("v6.intent_min_edge required>=5e-05, got 0", joined)
        self.assertIn("v6.hard_arb_min_net_edge required>=5e-05, got 4.9e-05", joined)

    def test_v6_kelly_trade_size_drawdown_and_paper_only_are_bounded(self) -> None:
        current = v6_envelope()
        current["fractional_kelly"] = 0.251
        current["max_trade_usd"] = 125.01
        current["max_drawdown"] = 0.151
        current["multi_strategy"]["global_max_drawdown"] = 0.151
        current["multi_strategy"]["paper_only"] = False
        current["v6"]["paper_only"] = False
        current["v6"]["hard_arb_max_trade_usd"] = 125.01
        joined = "\n".join(compare_paper_config(BASE, current, "config/paper_v6.json"))
        self.assertIn("fractional_kelly allowed<=0.25, got 0.251", joined)
        self.assertIn("max_trade_usd allowed<=125, got 125.01", joined)
        self.assertIn("max_drawdown allowed<=0.15, got 0.151", joined)
        self.assertIn("global_max_drawdown allowed<=0.15, got 0.151", joined)
        self.assertIn("multi_strategy.paper_only", joined)
        self.assertIn("v6.paper_only", joined)
        self.assertIn("v6.hard_arb_max_trade_usd allowed<=125, got 125.01", joined)

    def test_v6_authorized_allocations_sum_to_one_and_overallocation_is_rejected(self) -> None:
        current = v6_envelope()
        self.assertAlmostEqual(sum(current["v6"][key] for key in (
            "micro_maker_capital_fraction", "micro_taker_capital_fraction",
            "relative_value_capital_fraction", "hard_arb_capital_fraction",
            "external_capital_fraction", "reserve_fraction",
        )), 1.0)
        current["v6"]["reserve_fraction"] = 0.03
        errors = compare_paper_config(BASE, current, "config/paper_v6.json")
        self.assertTrue(any("allocations exceed 100%" in error for error in errors), errors)
        self.assertTrue(any("reserve_fraction allowed<=0.02, got 0.03" in error for error in errors), errors)

    def test_v6_each_sleeve_is_bounded_even_when_total_stays_one(self) -> None:
        current = v6_envelope()
        current["v6"]["relative_value_capital_fraction"] = 0.35
        current["v6"]["micro_maker_capital_fraction"] = 0.21
        joined = "\n".join(compare_paper_config(BASE, current, "config/paper_v6.json"))
        self.assertIn("relative_value_capital_fraction allowed<=0.34, got 0.35", joined)
        self.assertNotIn("allocations exceed 100%", joined)

    def test_v6_conservative_underallocation_is_allowed(self) -> None:
        current = v6_envelope()
        current["v6"]["relative_value_capital_fraction"] = 0.30
        self.assertEqual(compare_paper_config(BASE, current, "config/paper_v6.json"), [])

    def test_stricter_v6_safety_is_allowed(self) -> None:
        current = v6_envelope()
        current["max_market_fraction"] = 0.02
        current["max_event_fraction"] = 0.06
        current["max_gross_fraction"] = 0.40
        current["max_drawdown"] = 0.10
        current["multi_strategy"]["global_max_gross_fraction"] = 0.40
        current["multi_strategy"]["global_max_drawdown"] = 0.10
        current["multi_strategy"]["strategies"][0]["overrides"]["max_gross_fraction"] = 0.35
        current["multi_strategy"]["strategies"][0]["overrides"]["max_drawdown"] = 0.10
        self.assertEqual(compare_paper_config(BASE, current, "config/paper_v6.json"), [])

    def test_new_v7_can_use_operator_authorized_100_percent_ceiling_against_v6_incumbent(self) -> None:
        # This is the regression that prevents a V6 5/15/70 baseline from being
        # misread as the authorization for a new V7 config.
        self.assertEqual(compare_paper_config(v6_envelope(), v7_envelope(), "config/paper_v7.json"), [])

    def test_v7_bounded_policy_rollback_is_rejected_as_operator_directive_conflict(self) -> None:
        current = v7_envelope()
        current["fixed_dollar_trade_cap_enabled"] = True
        current["max_trade_usd"] = 125.0
        current["max_market_fraction"] = 0.05
        current["max_event_fraction"] = 0.15
        current["max_gross_fraction"] = 0.70
        current["multi_strategy"]["global_max_gross_fraction"] = 0.70
        current["v7"]["hard_arb_fixed_dollar_trade_cap_enabled"] = True
        current["v7"]["hard_arb_max_trade_usd"] = 125.0
        joined = "\n".join(compare_paper_config(v6_envelope(), current, "config/paper_v7.json"))
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
        current["fractional_kelly"] = 0.251
        current["max_drawdown"] = 0.151
        current["v7"]["authenticated_execution"] = True
        current["v7"]["authoritative_fee_required"] = False
        current["v7"]["shared_execution_ledger_required"] = False
        current["v7"]["joint_fill_state_required_for_multileg"] = False
        joined = "\n".join(compare_paper_config(v6_envelope(), current, "config/paper_v7.json"))
        self.assertIn("market_limit allowed<=1000, got 1001", joined)
        self.assertIn("min_liquidity required>=2, got 1.99", joined)
        self.assertIn("min_net_edge required>=5e-05, got 0", joined)
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
        joined = "\n".join(compare_paper_config(v6_envelope(), current, "config/paper_v7.json"))
        self.assertIn("max_trade_usd must be a nonbinding compatibility sentinel", joined)
        self.assertIn("hard_arb_max_trade_usd must be a nonbinding compatibility sentinel", joined)

    def test_runtime_hard_safety_surfaces_include_loop_and_materializer(self) -> None:
        self.assertTrue(is_runtime_hard_safety_surface("scripts/paper_v6_loop.sh"))
        self.assertTrue(is_runtime_hard_safety_surface("scripts/v6_materialize_configs.py"))
        self.assertTrue(is_runtime_hard_safety_surface("scripts/paper_v7_loop.sh"))
        self.assertFalse(is_runtime_hard_safety_surface("scripts/v6_hf_pressure_research.py"))

    def test_new_runtime_hard_safety_override_is_rejected(self) -> None:
        base = "child={k:x for k,x in cfg.items()}\n"
        current = base + "MAX_MARKET_FRACTION=0.06\n"
        errors = compare_runtime_hard_safety(base, current, "scripts/v6_materialize_configs.py")
        self.assertEqual(len(errors), 1)
        self.assertIn("MAX_MARKET_FRACTION", errors[0])

    def test_runtime_authorized_paper_aggression_without_hard_safety_write_is_allowed(self) -> None:
        base = "run_maker --min-edge 0.00035 --max-order-usd 25\n"
        current = "run_maker --min-edge 0.00005 --max-order-usd 125 --improve-ticks 1\n"
        self.assertEqual(compare_runtime_hard_safety(base, current, "scripts/paper_v6_loop.sh"), [])


if __name__ == "__main__":
    unittest.main()
