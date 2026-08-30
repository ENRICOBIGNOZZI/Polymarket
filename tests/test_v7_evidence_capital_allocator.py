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

    def test_no_terminal_evidence_preserves_exploration_and_reserves_exploitation(self) -> None:
        report = propose(self.allocation(), {})
        self.assertEqual(report["state"], "EXPLORATION_ONLY_MORE_TERMINAL_EVIDENCE")
        self.assertAlmostEqual(report["exploration_total"], 10.0)
        self.assertAlmostEqual(report["unallocated_exploitation_reserve"], 90.0)
        self.assertTrue(report["active_paper_envelopes_unchanged"])
        self.assertFalse(report["automatic_transfer"])

    def test_only_positive_full_cost_strategy_receives_proposed_exploitation(self) -> None:
        economics = {
            "expected_model_sha": "a" * 40,
            "strategy_mature_terminal_units": {"maker": 20, "arb": 20},
            "strategy_stressed_net_pnl": {
                "maker": {"2x": -1.0}, "arb": {"2x": 2.0},
            },
            "strategy_capital_hours": {"maker": 2.0, "arb": 1.0},
        }
        report = propose(self.allocation(), economics)
        self.assertEqual(report["state"], "EXPLOITATION_PROPOSED")
        self.assertEqual(report["strategies"]["maker"]["proposed_exploitation"], 0.0)
        self.assertAlmostEqual(report["strategies"]["arb"]["proposed_exploitation"], 90.0)
        self.assertAlmostEqual(report["proposed_allocated_total"], 100.0)


if __name__ == "__main__":
    unittest.main()
