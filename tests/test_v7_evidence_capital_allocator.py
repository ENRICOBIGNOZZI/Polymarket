from __future__ import annotations

import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

from v7_evidence_capital_allocator import propose  # noqa: E402


class EvidenceCapitalAllocatorTests(unittest.TestCase):
    def allocation(self):
        return {
            "paper_only": True, "authenticated_execution": False,
            "real_order_submission": False, "account_starting_capital": 100.0,
            "engine_budgets": {
                "BTC_SETTLEMENT_ENGINE": 50.0,
                "STRUCTURAL_ARB_ENGINE": 50.0,
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
            report["engines"]["BTC_SETTLEMENT_ENGINE"]["blocking_reasons"],
            [
                "INSUFFICIENT_TERMINAL_UNITS", "INSUFFICIENT_DAY_BLOCKS",
                "DAY_BLOCK_LCB95_NOT_POSITIVE", "FULL_COST_2X_PNL_NOT_POSITIVE",
                "CAPITAL_HOURS_MISSING", "CAPACITY_MISSING_OR_ZERO",
                "DRAWDOWN_MISSING",
            ],
        )

    def test_only_positive_robust_capacity_bounded_strategy_receives_exploitation(self) -> None:
        economics = {
            "expected_model_sha": "a" * 40,
            "engine_mature_terminal_units": {"BTC_SETTLEMENT_ENGINE": 400, "STRUCTURAL_ARB_ENGINE": 400},
            "engine_stressed_net_pnl": {
                "BTC_SETTLEMENT_ENGINE": {"2x": -1.0}, "STRUCTURAL_ARB_ENGINE": {"2x": 2.0},
            },
            "engine_capital_hours": {"BTC_SETTLEMENT_ENGINE": 2.0, "STRUCTURAL_ARB_ENGINE": 1.0},
            "engine_capacity_usd": {"BTC_SETTLEMENT_ENGINE": 20.0, "STRUCTURAL_ARB_ENGINE": 20.0},
            "engine_drawdown_usd": {"BTC_SETTLEMENT_ENGINE": 1.0, "STRUCTURAL_ARB_ENGINE": 1.0},
            "engine_day_stressed_net_pnl": {
                "BTC_SETTLEMENT_ENGINE": {f"2026-08-{day:02d}": -0.1 for day in range(1, 31)},
                "STRUCTURAL_ARB_ENGINE": {f"2026-08-{day:02d}": 0.1 for day in range(1, 31)},
            },
        }
        report = propose(self.allocation(), economics)
        self.assertEqual(report["state"], "MANUAL_EXPLOITATION_PROPOSAL")
        self.assertEqual(report["engines"]["BTC_SETTLEMENT_ENGINE"]["proposed_exploitation"], 0.0)
        # 85% stays cash; the information budget uses 10%, so only 5% is an
        # exploitation pool. Concentration caps one strategy at 25% of it.
        self.assertAlmostEqual(report["engines"]["STRUCTURAL_ARB_ENGINE"]["proposed_exploitation"], 1.25)
        self.assertAlmostEqual(report["proposed_allocated_total"], 11.25)
        self.assertAlmostEqual(report["unallocated_exploitation_reserve"], 88.75)
        self.assertGreater(
            report["engines"]["STRUCTURAL_ARB_ENGINE"]["day_block_confidence"]["lcb95"], 0.0
        )

    def test_drawdown_and_missing_capacity_fail_closed(self) -> None:
        economics = {
            "engine_mature_terminal_units": {"BTC_SETTLEMENT_ENGINE": 400},
            "engine_stressed_net_pnl": {"BTC_SETTLEMENT_ENGINE": {"2x": 10.0}},
            "engine_capital_hours": {"BTC_SETTLEMENT_ENGINE": 20.0},
            "engine_capacity_usd": {"BTC_SETTLEMENT_ENGINE": 0.0},
            "engine_drawdown_fraction": {"BTC_SETTLEMENT_ENGINE": 0.10},
            "engine_day_stressed_net_pnl": {
                "BTC_SETTLEMENT_ENGINE": [0.1] * 30,
            },
        }
        report = propose(self.allocation(), economics)
        reasons = report["engines"]["BTC_SETTLEMENT_ENGINE"]["blocking_reasons"]
        self.assertIn("CAPACITY_MISSING_OR_ZERO", reasons)
        self.assertIn("HARD_DRAWDOWN_BREACH", reasons)
        self.assertEqual(report["proposed_exploitation_total"], 0.0)


if __name__ == "__main__":
    unittest.main()
