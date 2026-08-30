#!/usr/bin/env python3
from __future__ import annotations

import json
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

from v7_portfolio_guard import assess


class PortfolioGuardTests(unittest.TestCase):
    def test_fast_structural_executor_equity_is_not_reserved(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            alloc = root / "manifest.json"
            alloc.write_text(json.dumps({
                "account_starting_capital": 100.0,
                "budgets": {"fast_structural": 60.0, "reserve": 40.0},
            }))
            status = root / "fast_structural" / "paper_executor_status.json"
            status.parent.mkdir(parents=True)
            status.write_text(json.dumps({
                "paper_only": True, "authenticated_execution": False,
                "equity": 55.0, "killed": False,
            }))
            report = assess(root, alloc, max_drawdown=.15)
            self.assertFalse(report["killed"])
            self.assertEqual(report["sleeves"]["fast_structural"]["source"], "reported")
            self.assertAlmostEqual(report["equity"], 95.0)

    def test_account_drawdown_triggers_global_kill(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            alloc = root / "control" / "allocations" / "manifest.json"
            alloc.parent.mkdir(parents=True)
            alloc.write_text(json.dumps({"account_starting_capital": 100.0, "budgets": {"graph_rv": 50.0, "reserve": 50.0}}))
            status = root / "graph_rv" / "status.json"
            status.parent.mkdir(parents=True)
            status.write_text(json.dumps({"paper_only": True, "authenticated_execution": False, "equity": 30.0, "killed": False}))
            report = assess(root, alloc, max_drawdown=.15)
            self.assertTrue(report["killed"])
            self.assertAlmostEqual(report["equity"], 80.0)
            self.assertTrue((root / "control" / "KILL").exists())

    def test_missing_inactive_sleeve_keeps_reserved_budget(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            alloc = root / "manifest.json"
            alloc.write_text(json.dumps({"account_starting_capital": 100.0, "budgets": {"external": 100.0}}))
            report = assess(root, alloc, max_drawdown=.15)
            self.assertFalse(report["killed"])
            self.assertEqual(report["equity"], 100.0)

    def test_external_paper_router_equity_joins_global_guard(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            alloc = root / "manifest.json"
            alloc.write_text(json.dumps({"account_starting_capital": 100.0, "budgets": {"external": 60.0, "reserve": 40.0}}))
            status = root / "external_fair" / "paper_router_status.json"
            status.parent.mkdir(parents=True)
            status.write_text(json.dumps({
                "paper_only": True, "authenticated_execution": False,
                "equity": 55.0, "killed": False,
            }))
            report = assess(root, alloc, max_drawdown=.15)
            self.assertFalse(report["killed"])
            self.assertEqual(report["sleeves"]["external"]["source"], "reported")
            self.assertAlmostEqual(report["equity"], 95.0)

    def test_professional_maker_equity_is_not_treated_as_reserved(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            alloc = root / "manifest.json"
            alloc.write_text(json.dumps({"account_starting_capital": 100.0, "budgets": {"micro_maker": 60.0, "reserve": 40.0}}))
            status = root / "micro_maker" / "status.json"
            status.parent.mkdir(parents=True)
            status.write_text(json.dumps({
                "paper_only": True,
                "authenticated_execution": False,
                "equity": 55.0,
                "killed": False,
            }))
            report = assess(root, alloc, max_drawdown=.15)
            self.assertFalse(report["killed"])
            self.assertEqual(report["sleeves"]["micro_maker"]["source"], "reported")
            self.assertAlmostEqual(report["equity"], 95.0)

    def test_unsafe_or_unmarkable_maker_kills_account(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            alloc = root / "manifest.json"
            alloc.write_text(json.dumps({"account_starting_capital": 100.0, "budgets": {"micro_maker": 60.0, "reserve": 40.0}}))
            status = root / "micro_maker" / "status.json"
            status.parent.mkdir(parents=True)
            status.write_text(json.dumps({
                "paper_only": True,
                "authenticated_execution": False,
                "equity": 0.0,
                "killed": True,
                "source": "fail_closed_unmarkable",
            }))
            report = assess(root, alloc, max_drawdown=.15)
            self.assertTrue(report["killed"])
            self.assertTrue((root / "control" / "KILL").exists())

    def test_locally_killed_sleeve_is_quarantined_without_global_kill(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            alloc = root / "manifest.json"
            alloc.write_text(json.dumps({
                "account_starting_capital": 100.0,
                "budgets": {"micro_taker": 20.0, "reserve": 80.0},
            }))
            status = root / "micro_taker" / "status.json"
            status.parent.mkdir(parents=True)
            status.write_text(json.dumps({
                "paper_only": True, "authenticated_execution": False,
                "equity": 17.0, "killed": True,
            }))
            report = assess(root, alloc, max_drawdown=.15)
            self.assertFalse(report["killed"])
            self.assertEqual(report["locally_killed_sleeves"], ["micro_taker"])
            self.assertEqual(report["fatal_sleeves"], [])
            self.assertFalse((root / "control" / "KILL").exists())


if __name__ == "__main__":
    unittest.main()
