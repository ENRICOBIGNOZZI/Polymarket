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


if __name__ == "__main__":
    unittest.main()
