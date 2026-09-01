from __future__ import annotations

import json
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

from v7_evidence_capital_allocator import propose, validate_policy  # noqa: E402


class EvidenceCapitalAllocatorTests(unittest.TestCase):
    def policy(self):
        return json.loads((ROOT / "config/v7_economic_readiness.json").read_text())

    def test_checked_in_economic_policy_preserves_required_floors(self) -> None:
        validate_policy(self.policy())

    def allocation(self):
        return {
            "paper_only": True, "authenticated_execution": False,
            "real_order_submission": False, "account_starting_capital": 100.0,
            "engine_budgets": {
                "CRYPTO_SETTLEMENT_ENGINE": 50.0,
                "STRUCTURAL_ARB_ENGINE": 50.0,
            },
        }

    def robust_dimensions(self):
        attribution = [
            "engine", "action", "component_provenance", "market", "horizon",
            "latency_regime", "fill_path", "cost_component",
        ]
        def row(action):
            return {
                "complete_cost_terminal_units": 400,
                "conditional_calibration_stable": True,
                "conditional_calibration_count": 400,
                "regime_stratification_complete": True,
                "source_health_stratification_complete": True,
                "attribution_dimensions": attribution,
                "applicable_action_classes": [action],
                "action_classes": {action: {
                    "mature_terminal_units": 400,
                    "complete_cost_vector": True,
                    "capital_hours": 1.0,
                    "capacity_usd": 20.0,
                    "drawdown_observed": True,
                    "positive_day_block_lcb": True,
                    "positive_2x_full_cost_pnl": True,
                    "conditional_calibration_stable": True,
                    "regime_stratified": True,
                    "source_health_stratified": True,
                }},
            }
        return {
            "CRYPTO_SETTLEMENT_ENGINE": row("TAKE"),
            "STRUCTURAL_ARB_ENGINE": row("ARB"),
        }

    def benchmark_comparison(self):
        names = {
            "polymarket_mid_diagnostic", "oracle_only_structural_model",
            "external_composite_plus_oracle", "settlement_model",
            "settlement_plus_microstructure", "unified_make_take_nothing_policy",
        }
        return {
            "policy_observation_cut_frozen": True,
            "trade_reselection_under_stress": False,
            "benchmarks": {
                name: {"causal": True, "observation_count": 400}
                for name in names
            },
        }

    def test_no_terminal_evidence_preserves_information_budget_and_cash(self) -> None:
        report = propose(self.allocation(), {})
        self.assertEqual(report["state"], "INFORMATION_ONLY_CASH_DEFAULT")
        self.assertAlmostEqual(report["information_budget_total"], 10.0)
        self.assertAlmostEqual(report["unallocated_exploitation_reserve"], 90.0)
        self.assertTrue(report["active_paper_envelopes_unchanged"])
        self.assertFalse(report["automatic_transfer"])
        self.assertTrue(report["manual_promotion_artifact_required"])
        self.assertEqual(
            report["engines"]["CRYPTO_SETTLEMENT_ENGINE"]["blocking_reasons"],
            [
                "INSUFFICIENT_TERMINAL_UNITS", "COMPLETE_COST_TERMINAL_UNITS_MISSING",
                "INSUFFICIENT_DAY_BLOCKS", "DAY_BLOCK_LCB95_NOT_POSITIVE", "FULL_COST_2X_PNL_NOT_POSITIVE",
                "CAPITAL_HOURS_MISSING", "CAPACITY_MISSING_OR_ZERO",
                "DRAWDOWN_MISSING", "CONDITIONAL_CALIBRATION_NOT_STABLE",
                "REGIME_STRATIFICATION_INCOMPLETE", "SOURCE_HEALTH_STRATIFICATION_INCOMPLETE",
                "ACTION_CLASS_EVIDENCE_INCOMPLETE", "ATTRIBUTION_DIMENSIONS_INCOMPLETE",
                "CAUSAL_BENCHMARK_COMPARISON_INCOMPLETE", "SETTLEMENT_LABELED_DAYS_BELOW_30",
                "SETTLEMENT_LABELED_CONTRACTS_BELOW_2500", "FORWARD_OOS_POLICY_TRADES_BELOW_300",
                "SETTLEMENT_CONDITIONAL_CALIBRATION_INSUFFICIENT", "SETTLEMENT_UNCERTAINTY_NOT_BELOW_EDGE",
            ],
        )
        self.assertEqual(report["technical_readiness"], "GREEN")
        self.assertEqual(report["economic_readiness"], "RED")

    def test_only_positive_robust_capacity_bounded_strategy_receives_exploitation(self) -> None:
        economics = {
            "expected_model_sha": "a" * 40,
            "engine_mature_terminal_units": {"CRYPTO_SETTLEMENT_ENGINE": 400, "STRUCTURAL_ARB_ENGINE": 400},
            "engine_stressed_net_pnl": {
                "CRYPTO_SETTLEMENT_ENGINE": {"2x": -1.0}, "STRUCTURAL_ARB_ENGINE": {"2x": 2.0},
            },
            "engine_capital_hours": {"CRYPTO_SETTLEMENT_ENGINE": 2.0, "STRUCTURAL_ARB_ENGINE": 1.0},
            "engine_capacity_usd": {"CRYPTO_SETTLEMENT_ENGINE": 20.0, "STRUCTURAL_ARB_ENGINE": 20.0},
            "engine_drawdown_usd": {"CRYPTO_SETTLEMENT_ENGINE": 1.0, "STRUCTURAL_ARB_ENGINE": 1.0},
            "engine_day_stressed_net_pnl": {
                "CRYPTO_SETTLEMENT_ENGINE": {f"2026-08-{day:02d}": -0.1 for day in range(1, 31)},
                "STRUCTURAL_ARB_ENGINE": {f"2026-08-{day:02d}": 0.1 for day in range(1, 31)},
            },
            "engine_evidence_dimensions": self.robust_dimensions(),
            "engine_settlement_model_evidence": {"CRYPTO_SETTLEMENT_ENGINE": {
                "settlement_labeled_days": 30,
                "settlement_labeled_contracts": 2500,
                "forward_oos_policy_trades": 300,
                "minimum_conditional_calibration_bin_count": 40,
                "uncertainty_upper": 0.01,
                "claimed_edge_lower": 0.02,
            }},
            "benchmark_policy_comparison": self.benchmark_comparison(),
        }
        report = propose(self.allocation(), economics, policy=self.policy())
        self.assertEqual(report["state"], "MANUAL_EXPLOITATION_PROPOSAL")
        self.assertEqual(report["engines"]["CRYPTO_SETTLEMENT_ENGINE"]["proposed_exploitation"], 0.0)
        # 85% stays cash; the information budget uses 10%, so only 5% is an
        # exploitation pool. Concentration caps one strategy at 25% of it.
        self.assertAlmostEqual(report["engines"]["STRUCTURAL_ARB_ENGINE"]["proposed_exploitation"], 1.25)
        self.assertAlmostEqual(report["proposed_allocated_total"], 11.25)
        self.assertAlmostEqual(report["unallocated_exploitation_reserve"], 88.75)
        self.assertGreater(
            report["engines"]["STRUCTURAL_ARB_ENGINE"]["day_block_confidence"]["lcb95"], 0.0
        )
        self.assertTrue(report["economic_readiness_policy_applied"])

    def test_drawdown_and_missing_capacity_fail_closed(self) -> None:
        economics = {
            "engine_mature_terminal_units": {"CRYPTO_SETTLEMENT_ENGINE": 400},
            "engine_stressed_net_pnl": {"CRYPTO_SETTLEMENT_ENGINE": {"2x": 10.0}},
            "engine_capital_hours": {"CRYPTO_SETTLEMENT_ENGINE": 20.0},
            "engine_capacity_usd": {"CRYPTO_SETTLEMENT_ENGINE": 0.0},
            "engine_drawdown_fraction": {"CRYPTO_SETTLEMENT_ENGINE": 0.10},
            "engine_day_stressed_net_pnl": {
                "CRYPTO_SETTLEMENT_ENGINE": [0.1] * 30,
            },
            "engine_evidence_dimensions": self.robust_dimensions(),
            "engine_settlement_model_evidence": {"CRYPTO_SETTLEMENT_ENGINE": {
                "settlement_labeled_days": 30, "settlement_labeled_contracts": 2500,
                "forward_oos_policy_trades": 300, "minimum_conditional_calibration_bin_count": 40,
                "uncertainty_upper": 0.01, "claimed_edge_lower": 0.02,
            }},
            "benchmark_policy_comparison": self.benchmark_comparison(),
        }
        report = propose(self.allocation(), economics)
        reasons = report["engines"]["CRYPTO_SETTLEMENT_ENGINE"]["blocking_reasons"]
        self.assertIn("CAPACITY_MISSING_OR_ZERO", reasons)
        self.assertIn("HARD_DRAWDOWN_BREACH", reasons)
        self.assertEqual(report["proposed_exploitation_total"], 0.0)


if __name__ == "__main__":
    unittest.main()
