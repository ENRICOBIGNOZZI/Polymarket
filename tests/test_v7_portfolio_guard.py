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


def lead_lag(root: Path, *, realized: float, open_costs: list[tuple[float, float]] = (),
             settled_pnls: list[float] = ()) -> None:
    directory = root / "research/lead_lag_taker_v1"; directory.mkdir(parents=True, exist_ok=True)
    protocol = "b" * 64; sha = "a" * 40
    positions = {}
    for index, pnl in enumerate(settled_pnls):
        positions[f"settled-{index}"] = {
            "protocol_hash": protocol, "settled": True, "final_pnl": pnl,
        }
    for index, (cost, fee) in enumerate(open_costs):
        positions[f"open-{index}"] = {
            "protocol_hash": protocol, "settled": False,
            "entry_cost": cost, "entry_fee": fee,
        }
    (directory / "status.json").write_text(json.dumps({
        "schema": "polymarket_v7_lead_lag_taker_v1_status",
        "paper_only": True, "authenticated_execution": False,
        "real_order_submission": False, "real_capital_at_risk": False,
        "model_sha": sha, "protocol_hash": protocol,
        "entries": len(positions), "settled": len(settled_pnls),
        "open_positions": len(open_costs), "realized_pnl": realized,
    }))
    (directory / "state.json").write_text(json.dumps({
        "model_sha": sha, "protocol_hash": protocol,
        "realized_pnl": realized, "positions": positions,
    }))


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

    def test_lead_lag_realized_pnl_is_part_of_crypto_engine_equity(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            btc = root / "external_fair/paper_router_status.json"; btc.parent.mkdir(parents=True)
            btc.write_text(json.dumps({"paper_only": True, "authenticated_execution": False,
                                      "real_order_submission": False, "equity": 55.0, "killed": False}))
            structural = root / "hard_arb/status.json"; structural.parent.mkdir(parents=True)
            structural.write_text(json.dumps({"paper_only": True, "authenticated_execution": False,
                "real_order_submission": False, "equity_cost_basis": 18.0, "killed": False}))
            lead_lag(root, realized=5.0, settled_pnls=[2.0, 3.0])
            report = assess(root, allocation(root / "manifest.json"), max_drawdown=.15)
            crypto = report["engines"]["CRYPTO_SETTLEMENT_ENGINE"]
            self.assertEqual(crypto["equity"], 60.0)
            self.assertEqual(report["equity"], 98.0)
            self.assertEqual(crypto["components"]["lead_lag_taker_v1"]["realized_pnl"], 5.0)

    def test_open_lead_lag_position_uses_zero_recovery_value_risk_mark(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            btc = root / "external_fair/paper_router_status.json"; btc.parent.mkdir(parents=True)
            btc.write_text(json.dumps({"paper_only": True, "authenticated_execution": False,
                                      "real_order_submission": False, "equity": 55.0, "killed": False}))
            lead_lag(root, realized=0.0, open_costs=[(10.0, 1.0)])
            report = assess(root, allocation(root / "manifest.json"), max_drawdown=.50)
            crypto = report["engines"]["CRYPTO_SETTLEMENT_ENGINE"]
            self.assertEqual(crypto["equity"], 44.0)
            self.assertEqual(crypto["components"]["lead_lag_taker_v1"]["open_cost_at_risk"], 11.0)
            self.assertIn("conservative_open_mark", crypto["source"])

    def test_unreconciled_lead_lag_state_is_fatal_not_silently_ignored(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            lead_lag(root, realized=1.0, settled_pnls=[1.0])
            state_path = root / "research/lead_lag_taker_v1/state.json"
            state = json.loads(state_path.read_text()); state["realized_pnl"] = 999.0
            state_path.write_text(json.dumps(state))
            report = assess(root, allocation(root / "manifest.json"), max_drawdown=.90)
            self.assertTrue(report["killed"])
            self.assertIn("CRYPTO_SETTLEMENT_ENGINE", report["fatal_engines"])
            self.assertEqual(report["engines"]["CRYPTO_SETTLEMENT_ENGINE"]["source"],
                             "lead_lag_unreconciled_fail_closed")

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
            self.assertNotIn("sleeves", report)
            self.assertEqual(set(report["engines"]), {"CRYPTO_SETTLEMENT_ENGINE", "STRUCTURAL_ARB_ENGINE"})

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
