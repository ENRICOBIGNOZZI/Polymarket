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
from v7_execution_ledger import LedgerEvent  # noqa: E402


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


SHA = "a" * 40


def canonical_runtime(root: Path) -> None:
    path=root/"control/runtime_status.json"; path.parent.mkdir(parents=True,exist_ok=True)
    path.write_text(json.dumps({"schema":"polymarket_v7_runtime_status_v3","model_sha":SHA,
        "paper_only":True,"authenticated_execution":False,"real_order_submission":False}))


def write_ledger(root: Path, events: list[LedgerEvent]) -> None:
    path=root/"ledger/execution.jsonl"; path.parent.mkdir(parents=True,exist_ok=True)
    path.write_text("".join(json.dumps(event.to_dict(),sort_keys=True)+"\n" for event in events))


def fill(order: str, fill_id: str, *, price: float=.4, size: float=5.0, fee: float=.1,
         component: str="crypto_informed_taker") -> LedgerEvent:
    return LedgerEvent(event_type="FILL",strategy="CRYPTO_SETTLEMENT_ENGINE",model_sha=SHA,
        order_id=order,fill_id=fill_id,token_id="token",side="BUY",fill_price=price,
        filled_size=size,fee=fee,fee_source="TEST_AUTHORITATIVE",exchange_ts_ms=1000,
        receive_ts_ms=1001,recorded_ts_ms=1002,
        metadata={"component":component,"coordinator_receipt":{"owner":"V7_GLOBAL_PORTFOLIO_COORDINATOR"}})


def final(order: str, pnl: float, *, component: str="crypto_informed_taker",
          record_id: str | None=None) -> LedgerEvent:
    kwargs={} if record_id is None else {"record_id":record_id}
    return LedgerEvent(event_type="FINAL",strategy="CRYPTO_SETTLEMENT_ENGINE",model_sha=SHA,
        order_id=order,final_pnl=pnl,recorded_ts_ms=1003,
        metadata={"component":component,"coordinator_receipt":{"owner":"V7_GLOBAL_PORTFOLIO_COORDINATOR"}},**kwargs)


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

    def test_canonical_ledger_unifies_maker_and_lead_lag_pnl(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp); canonical_runtime(root)
            write_ledger(root,[fill("lead","f1",fee=.05),final("lead",4.6),
                               fill("maker","f2",price=.32,fee=0.0,component="professional_maker"),
                               final("maker",3.4,component="professional_maker")])
            report=assess(root,allocation(root/"manifest.json"),max_drawdown=.15)
            crypto=report["engines"]["CRYPTO_SETTLEMENT_ENGINE"]
            self.assertEqual(crypto["source"],"canonical_ledger_conservative")
            self.assertAlmostEqual(crypto["equity"],68.0)
            self.assertAlmostEqual(report["equity"],108.0)
            self.assertEqual(crypto["details"]["component_realized_pnl"],
                             {"crypto_informed_taker":4.6,"professional_maker":3.4})

    def test_canonical_open_fill_is_conservatively_debited(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp); canonical_runtime(root)
            write_ledger(root,[fill("open","f1",price=.4,size=5,fee=.1)])
            report=assess(root,allocation(root/"manifest.json"),max_drawdown=.15)
            crypto=report["engines"]["CRYPTO_SETTLEMENT_ENGINE"]
            self.assertAlmostEqual(crypto["equity"],57.9)
            self.assertAlmostEqual(report["equity"],97.9)
            self.assertEqual(crypto["details"]["open_order_count"],1)
            self.assertAlmostEqual(crypto["details"]["conservative_open_cost"],2.1)

    def test_duplicate_canonical_final_kills_account_instead_of_double_counting(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp); canonical_runtime(root)
            write_ledger(root,[fill("one","f1"),final("one",1.0,record_id="final-a"),
                               final("one",1.0,record_id="final-b")])
            report=assess(root,allocation(root/"manifest.json"),max_drawdown=.15)
            self.assertTrue(report["killed"])
            self.assertIn("CRYPTO_SETTLEMENT_ENGINE",report["fatal_engines"])
            self.assertEqual(report["engines"]["CRYPTO_SETTLEMENT_ENGINE"]["source"],
                             "canonical_ledger_duplicate")



if __name__ == "__main__":
    unittest.main()
