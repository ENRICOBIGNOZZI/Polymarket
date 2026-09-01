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
            "strategy_budgets": {"maker": 50.0, "arb": 50.0},
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
            report["strategies"]["maker"]["blocking_reasons"],
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
            "strategy_mature_terminal_units": {"maker": 400, "arb": 400},
            "strategy_stressed_net_pnl": {
                "maker": {"2x": -1.0}, "arb": {"2x": 2.0},
            },
            "strategy_capital_hours": {"maker": 2.0, "arb": 1.0},
            "strategy_capacity_usd": {"maker": 20.0, "arb": 20.0},
            "strategy_drawdown_usd": {"maker": 1.0, "arb": 1.0},
            "strategy_day_stressed_net_pnl": {
                "maker": {f"2026-08-{day:02d}": -0.1 for day in range(1, 31)},
                "arb": {f"2026-08-{day:02d}": 0.1 for day in range(1, 31)},
            },
        }
        report = propose(self.allocation(), economics)
        self.assertEqual(report["state"], "MANUAL_EXPLOITATION_PROPOSAL")
        self.assertEqual(report["strategies"]["maker"]["proposed_exploitation"], 0.0)
        # 85% stays cash; the information budget uses 10%, so only 5% is an
        # exploitation pool. Concentration caps one strategy at 25% of it.
        self.assertAlmostEqual(report["strategies"]["arb"]["proposed_exploitation"], 1.25)
        self.assertAlmostEqual(report["proposed_allocated_total"], 11.25)
        self.assertAlmostEqual(report["unallocated_exploitation_reserve"], 88.75)
        self.assertGreater(
            report["strategies"]["arb"]["day_block_confidence"]["lcb95"], 0.0
        )

    def test_drawdown_and_missing_capacity_fail_closed(self) -> None:
        economics = {
            "strategy_mature_terminal_units": {"maker": 400},
            "strategy_stressed_net_pnl": {"maker": {"2x": 10.0}},
            "strategy_capital_hours": {"maker": 20.0},
            "strategy_capacity_usd": {"maker": 0.0},
            "strategy_drawdown_fraction": {"maker": 0.10},
            "strategy_day_stressed_net_pnl": {
                "maker": [0.1] * 30,
            },
        }
        report = propose(self.allocation(), economics)
        reasons = report["strategies"]["maker"]["blocking_reasons"]
        self.assertIn("CAPACITY_MISSING_OR_ZERO", reasons)
        self.assertIn("HARD_DRAWDOWN_BREACH", reasons)
        self.assertEqual(report["proposed_exploitation_total"], 0.0)


if __name__ == "__main__":
    unittest.main()
