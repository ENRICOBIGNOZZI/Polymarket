from __future__ import annotations

import json
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

from v7_execution_ledger import LedgerEvent, iter_records  # noqa: E402
from v7_ledger_spool import drain_spool, spool_event  # noqa: E402
from v7_paper_exploration_account import (  # noqa: E402
    STRATEGY,
    canonical_final_record_id,
    reconcile_once,
)

SHA = "c" * 40


def receipt(*, probe: bool = False) -> dict[str, object]:
    return {
        "schema": "polymarket_v7_global_opportunity_decision_v1",
        "owner": "V7_GLOBAL_PORTFOLIO_COORDINATOR",
        "engine_id": "CRYPTO_SETTLEMENT_ENGINE",
        "selected_replay_key": "crypto-settlement:BTC:M5:test",
        "action": "TAKE",
        "new_risk_authorized": False,
        "paper_exploration_authorized": True,
        "paper_exploration_probe_authorized": probe,
        "paper_only": True,
        "authenticated_execution": False,
        "real_order_submission": False,
        "real_capital_at_risk": False,
        "crypto_context": {
            "asset": "BTC",
            "horizon": "M5",
            "authority": "PAPER_EXPLORATION",
        },
    }


def metadata(*, probe: bool = False) -> dict[str, object]:
    return {
        "paper_exploration": True,
        "paper_bootstrap_probe": probe,
        "economic_authority": "PAPER_EXPLORATION",
        "counterfactual": False,
        "excluded_from_portfolio_equity": False,
        "research_evidence_only": False,
        "coordinator_receipt": receipt(probe=probe),
        "outcome": "YES",
        "fair_yes": 0.70,
        "arrival_pm_mid": 0.60,
    }


def prepare(root: Path) -> None:
    (root / "external_fair").mkdir(parents=True)
    (root / "control" / "allocations").mkdir(parents=True)
    (root / "control" / "allocations" / "manifest.json").write_text(
        json.dumps({
            "engine_budgets": {"CRYPTO_SETTLEMENT_ENGINE": 4000.0}
        }),
        encoding="utf-8",
    )


def order_and_fill(
    *,
    base_ms: int = 100_000,
    order_id: str = "order-1",
    fill_id: str = "fill-1",
    position_id: str = "position-1",
    market_id: str = "market-1",
    probe: bool = False,
) -> tuple[LedgerEvent, LedgerEvent]:
    common = metadata(probe=probe)
    order = LedgerEvent(
        event_type="ORDER_SUBMITTED",
        strategy=STRATEGY,
        model_sha=SHA,
        model_version="external-fair-structural-v7-paper",
        record_id=f"{order_id}-record",
        recorded_ts_ms=base_ms,
        candidate_id=f"candidate-{order_id}",
        order_id=order_id,
        position_id=position_id,
        market_id=market_id,
        event_id=f"event-{market_id}",
        token_id=f"token-{market_id}",
        side="BUY",
        exchange_ts_ms=base_ms - 2,
        receive_ts_ms=base_ms - 1,
        decision_ts_ms=base_ms,
        book_snapshot_id=f"book-{market_id}",
        limit_price=0.40,
        intended_action="TAKE",
        intended_size=10.0,
        order_state="SUBMITTED_SHADOW",
        metadata=common,
    )
    fill = LedgerEvent(
        event_type="FILL",
        strategy=STRATEGY,
        model_sha=SHA,
        model_version="external-fair-structural-v7-paper",
        record_id=f"{fill_id}-record",
        recorded_ts_ms=base_ms + 1,
        candidate_id=f"candidate-{order_id}",
        order_id=order_id,
        fill_id=fill_id,
        position_id=position_id,
        market_id=market_id,
        event_id=f"event-{market_id}",
        token_id=f"token-{market_id}",
        side="BUY",
        exchange_ts_ms=base_ms - 2,
        receive_ts_ms=base_ms - 1,
        fill_price=0.40,
        filled_size=10.0,
        complete=True,
        fee=0.20,
        fee_rate=0.01,
        fee_source="GAMMA_AUTHORITATIVE_FEE_SCHEDULE",
        slippage=0.0,
        metadata=common,
    )
    return order, fill


def append_virtual(root: Path, row: dict[str, object]) -> None:
    path = root / "external_fair" / "counterfactuals.jsonl"
    with path.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(row, sort_keys=True) + "\n")


def virtual_final(
    *,
    timestamp_ms: int = 100_100,
    payout: float = 10.0,
    pnl: float = 5.8,
) -> dict[str, object]:
    return {
        "schema": "polymarket_v7_external_fair_counterfactual_v1",
        "record_id": "virtual-final-1",
        "event_type": "VIRTUAL_FINAL",
        "timestamp_ms": timestamp_ms,
        "model_sha": SHA,
        "fill_id": "fill-1",
        "position_id": "position-1",
        "market_id": "market-1",
        "event_id": "event-market-1",
        "token_id": "token-market-1",
        "counterfactual_pnl": pnl,
        "virtual_cashflow": payout,
        "capital_duration_ms": 100,
        "metadata": {
            "won": True,
            "winning_token_id": "token-market-1",
            "settlement_outcome": "YES",
            "hold_to_settlement": True,
        },
    }


class PaperExplorationAccountTests(unittest.TestCase):
    def test_settlement_is_idempotent_restart_safe_and_uses_gross_payout(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            prepare(root)
            order, fill = order_and_fill()
            spool_event(root, order)
            spool_event(root, fill)
            self.assertEqual(drain_spool(root, model_sha=SHA)["appended"], 2)
            append_virtual(root, virtual_final())

            first = reconcile_once(root, SHA, current_ms=100_200)
            self.assertTrue(first["complete"])
            self.assertEqual(
                first["final_reconciliation"]["spooled_this_pass"], 1
            )
            account = first["account"]
            self.assertEqual(account["orders_submitted"], 1)
            self.assertEqual(account["fills"], 1)
            self.assertEqual(account["terminal_positions"], 1)
            self.assertEqual(account["open_positions"], 0)
            self.assertAlmostEqual(account["cash"], 4005.8)
            self.assertAlmostEqual(account["realized_pnl"], 5.8)

            second = reconcile_once(root, SHA, current_ms=100_300)
            self.assertTrue(second["complete"])
            self.assertEqual(
                second["final_reconciliation"]["spooled_this_pass"], 0
            )
            self.assertEqual(len(list((root / "ledger/spool").glob("*.json"))), 1)
            self.assertEqual(drain_spool(root, model_sha=SHA)["appended"], 1)

            restarted = reconcile_once(root, SHA, current_ms=100_400)
            self.assertTrue(restarted["complete"])
            self.assertAlmostEqual(restarted["account"]["cash"], 4005.8)
            finals = [
                event for event in iter_records(root / "ledger/execution.jsonl")
                if isinstance(event, LedgerEvent) and event.event_type == "FINAL"
            ]
            self.assertEqual(len(finals), 1)
            self.assertEqual(
                finals[0].record_id, canonical_final_record_id(SHA, "fill-1")
            )
            self.assertAlmostEqual(finals[0].realized_cashflow or 0.0, 10.0)
            self.assertAlmostEqual(finals[0].final_pnl or 0.0, 5.8)
            self.assertTrue(finals[0].metadata["cash_identity_verified"])

    def test_orphan_order_becomes_nonfill_without_fabricating_position(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            prepare(root)
            order, _ = order_and_fill(base_ms=1_000)
            spool_event(root, order)
            self.assertEqual(drain_spool(root, model_sha=SHA)["appended"], 1)

            status = reconcile_once(
                root, SHA, current_ms=10_000, orphan_grace_ms=1_000
            )
            self.assertTrue(status["complete"])
            self.assertEqual(
                status["order_reconciliation"]["terminal_nonfills"], 1
            )
            account = status["account"]
            self.assertEqual(account["fills"], 0)
            self.assertEqual(account["terminal_nonfills"], 1)
            self.assertEqual(account["open_positions"], 0)
            self.assertEqual(account["cash"], 4000.0)
            self.assertEqual(drain_spool(root, model_sha=SHA)["appended"], 1)
            events = list(iter_records(root / "ledger/execution.jsonl"))
            terminal = next(
                event for event in events
                if isinstance(event, LedgerEvent)
                and event.event_type == "ORDER_STATE"
            )
            self.assertEqual(terminal.order_state, "NONFILL")
            self.assertTrue(terminal.metadata["no_fill_fabricated"])

    def test_invalid_settlement_cash_identity_blocks_and_preserves_open_fill(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            prepare(root)
            order, fill = order_and_fill()
            spool_event(root, order)
            spool_event(root, fill)
            self.assertEqual(drain_spool(root, model_sha=SHA)["appended"], 2)
            append_virtual(root, virtual_final(payout=10.0, pnl=6.8))

            status = reconcile_once(root, SHA, current_ms=100_200)
            self.assertFalse(status["complete"])
            self.assertIn(
                "PAPER_EXPLORATION_FINAL_RECONCILIATION_INCOMPLETE",
                status["blockers"],
            )
            self.assertEqual(
                status["final_reconciliation"]["invalid_virtual_finals"],
                ["fill-1"],
            )
            self.assertEqual(status["account"]["open_positions"], 1)
            self.assertEqual(len(list((root / "ledger/spool").glob("*.json"))), 0)

    def test_latest_executable_mark_values_open_inventory_conservatively(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            prepare(root)
            order, fill = order_and_fill()
            spool_event(root, order)
            spool_event(root, fill)
            self.assertEqual(drain_spool(root, model_sha=SHA)["appended"], 2)
            append_virtual(root, {
                "schema": "polymarket_v7_external_fair_counterfactual_v1",
                "record_id": "mark-1",
                "event_type": "VIRTUAL_MARKOUT",
                "timestamp_ms": 100_050,
                "model_sha": SHA,
                "fill_id": "fill-1",
                "position_id": "position-1",
                "market_id": "market-1",
                "executable_liquidation_value": 4.0,
            })
            append_virtual(root, {
                "schema": "polymarket_v7_external_fair_counterfactual_v1",
                "record_id": "mark-2",
                "event_type": "VIRTUAL_MARKOUT",
                "timestamp_ms": 100_060,
                "model_sha": SHA,
                "fill_id": "fill-1",
                "position_id": "position-1",
                "market_id": "market-1",
                "executable_liquidation_value": 4.1,
            })

            status = reconcile_once(root, SHA, current_ms=100_200)
            self.assertTrue(status["complete"])
            account = status["account"]
            self.assertEqual(account["open_positions"], 1)
            self.assertAlmostEqual(account["cash"], 3995.8)
            self.assertAlmostEqual(account["marked_open_value"], 4.1)
            self.assertAlmostEqual(account["equity"], 3999.9)
            self.assertAlmostEqual(account["unrealized_pnl"], -0.1)


if __name__ == "__main__":
    unittest.main()
