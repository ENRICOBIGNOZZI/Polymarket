#!/usr/bin/env python3
from __future__ import annotations

import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "monitoring"))

from v7_portfolio_reconciliation import reconcile


class PortfolioReconciliationTests(unittest.TestCase):
    def inputs(self):
        return {
            "canonical": {
                "net_pnl": 3.0,
                "strategy_net_pnl": {"HARD_ARB": 1.0, "MICRO_TAKER": 2.0},
            },
            "ledger": {"total": {"final_pnl": 3.0}},
            "portfolio": {
                "equity": 103.0,
                "sleeves": {
                    "hard_arb": {"equity": 51.0},
                    "micro_taker": {"equity": 22.0},
                    "reserve": {"equity": 30.0},
                },
            },
            "allocations": {
                "account_starting_capital": 100.0,
                "budgets": {"hard_arb": 50.0, "micro_taker": 20.0, "reserve": 30.0},
            },
            "state_realized_pnl": {"HARD_ARB": 1.0, "MICRO_TAKER": 2.0},
        }

    def test_all_accounting_surfaces_reconcile(self) -> None:
        report = reconcile(**self.inputs())
        self.assertTrue(report["reconciled"])
        self.assertEqual(report["reason_codes"], [])

    def test_strategy_and_portfolio_divergence_fail_closed(self) -> None:
        values = self.inputs()
        values["state_realized_pnl"]["HARD_ARB"] = 9.0
        values["portfolio"]["equity"] = 999.0
        report = reconcile(**values)
        self.assertFalse(report["reconciled"])
        self.assertIn("portfolio_sleeve_equity_divergence", report["reason_codes"])
        self.assertIn("strategy_realized_pnl_divergence:HARD_ARB", report["reason_codes"])

    def test_raw_terminal_and_canonical_economic_pnl_cannot_disagree_silently(self) -> None:
        values = self.inputs()
        values["ledger"]["total"]["final_pnl"] = -4.0
        report = reconcile(**values)
        self.assertFalse(report["reconciled"])
        self.assertIn("ledger_canonical_terminal_pnl_divergence", report["reason_codes"])

    def test_empty_realized_evidence_is_reconciled_at_zero(self) -> None:
        values = self.inputs()
        values["canonical"] = {"net_pnl": None, "strategy_net_pnl": {}}
        values["ledger"] = {"total": {"final_pnl": 0.0}}
        values["state_realized_pnl"] = {}
        report = reconcile(**values)
        self.assertTrue(report["reconciled"])


if __name__ == "__main__":
    unittest.main()
