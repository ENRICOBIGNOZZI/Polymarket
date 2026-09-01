#!/usr/bin/env python3
from __future__ import annotations

import json
import sys
import tempfile
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))
from v7_portfolio_guard import assess  # noqa: E402


def allocation(path: Path, btc: float = 60.0, structural: float = 20.0, reserve: float = 20.0) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({
        "schema": "polymarket_v7_capital_allocation_v3",
        "paper_only": True, "authenticated_execution": False,
        "real_order_submission": False,
        "capital_authority_owner": "V7_CANONICAL_ALLOCATOR",
        "capital_authority_owner_count": 1,
        "account_starting_capital": btc + structural + reserve,
        "engine_budgets": {
            "CRYPTO_SETTLEMENT_ENGINE": btc,
            "STRUCTURAL_ARB_ENGINE": structural,
        },
        "reserve_budget": reserve,
    }))
    return path


class PortfolioGuardTests(unittest.TestCase):
    def test_two_engine_equity_is_accounted_once(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            btc = root / "external_fair/paper_router_status.json"
            btc.parent.mkdir(parents=True)
            btc.write_text(json.dumps({
                "paper_only": True, "authenticated_execution": False,
                "real_order_submission": False, "equity": 55.0, "killed": False,
            }))
            structural = root / "hard_arb/status.json"
            structural.parent.mkdir(parents=True)
            structural.write_text(json.dumps({
                "paper_only": True, "authenticated_execution": False,
                "real_order_submission": False, "equity_cost_basis": 18.0, "killed": False,
            }))
            report = assess(root, allocation(root / "manifest.json"), max_drawdown=.15)
            self.assertFalse(report["killed"])
            self.assertEqual(report["engines"]["CRYPTO_SETTLEMENT_ENGINE"]["source"], "reported")
            self.assertEqual(report["engines"]["STRUCTURAL_ARB_ENGINE"]["source"], "reported")
            self.assertEqual(report["equity"], 93.0)

    def test_account_drawdown_triggers_global_kill(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            btc = root / "external_fair/paper_router_status.json"
            btc.parent.mkdir(parents=True)
            btc.write_text(json.dumps({
                "paper_only": True, "authenticated_execution": False,
                "equity": 30.0, "killed": False,
            }))
            report = assess(root, allocation(root / "manifest.json", 60, 20, 20), max_drawdown=.15)
            self.assertTrue(report["killed"])
            self.assertEqual(report["equity"], 70.0)
            self.assertTrue((root / "control/KILL").exists())

    def test_missing_engine_status_preserves_its_envelope(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            report = assess(root, allocation(root / "manifest.json"), max_drawdown=.15)
            self.assertFalse(report["killed"])
            self.assertEqual(report["equity"], 100.0)
            self.assertEqual(report["engines"]["CRYPTO_SETTLEMENT_ENGINE"]["source"], "not_started")

    def test_component_observer_equity_never_enters_account_equity(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            maker = root / "micro_maker/status.json"
            maker.parent.mkdir(parents=True)
            maker.write_text(json.dumps({
                "paper_only": True, "authenticated_execution": False,
                "equity": 9999.0, "killed": False,
            }))
            report = assess(root, allocation(root / "manifest.json"), max_drawdown=.15)
            self.assertEqual(report["equity"], 100.0)
            self.assertEqual(report["sleeves"]["micro_maker"]["budget"], 0.0)
            self.assertEqual(report["sleeves"]["micro_maker"]["source"], "zero_authority_budget")

    def test_unsafe_or_unmarkable_engine_kills_account(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            status = root / "external_fair/paper_router_status.json"
            status.parent.mkdir(parents=True)
            status.write_text(json.dumps({
                "paper_only": True, "authenticated_execution": True,
                "equity": 60.0, "killed": False,
            }))
            report = assess(root, allocation(root / "manifest.json"), max_drawdown=.15)
            self.assertTrue(report["killed"])
            self.assertEqual(report["fatal_engines"], ["CRYPTO_SETTLEMENT_ENGINE"])


if __name__ == "__main__":
    unittest.main()
