from __future__ import annotations

from dataclasses import replace
import json
from pathlib import Path
import sys
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))
sys.path.insert(0, str(ROOT / "tests"))

from test_v7_multi_crypto_forward_freeze import draft
from v7_execution_ledger import LedgerEvent
from v7_lead_lag_replay import ReplayError
from v7_multi_crypto_forward_freeze import freeze
from v7_multi_crypto_forward_report import summarize

SHA = "a" * 40
BASE = 1_800_000_000_000


def cohort_events(*, final: bool = True, pnl: float = 1.0, market: str = "market-1", shock: str = "shock-1"):
    manifest = freeze(draft())
    packet = manifest["multi_crypto_forward"]
    replay = "replay-" + market
    receipt = {
        "schema": "polymarket_v7_global_opportunity_decision_v1",
        "owner": "V7_GLOBAL_PORTFOLIO_COORDINATOR", "action": "TAKE",
        "engine_id": "CRYPTO_SETTLEMENT_ENGINE",
        "crypto_context": {"asset": packet["asset"], "horizon": packet["horizon"],
                           "authority": "PAPER_EXPLORATION"},
        "selected_replay_key": replay, "new_risk_authorized": False,
        "paper_exploration_authorized": True, "paper_multi_crypto_forward_authorized": True,
        "paper_only": True, "authenticated_execution": False,
        "real_order_submission": False, "real_capital_at_risk": False,
        "multi_crypto_forward": packet,
    }
    common_md = {
        "paper_exploration": True, "economic_authority": "PAPER_EXPLORATION",
        "paper_multi_crypto_forward": True, "multi_crypto_forward": packet,
        "coordinator_receipt": receipt,
    }
    reserve_md = dict(common_md)
    reserve_md["reservation_projection"] = {
        "reservation_id": "reservation-" + market,
        "request": {"parent_shock_id": shock},
    }
    reserve = LedgerEvent(
        event_type="CAPITAL_RESERVE", strategy="CRYPTO_SETTLEMENT_ENGINE", model_sha=SHA,
        record_id="reserve-" + market, recorded_ts_ms=BASE,
        order_id="order-" + market, market_id=market, token_id="token-" + market,
        metadata=reserve_md,
    )
    order = LedgerEvent(
        event_type="ORDER_SUBMITTED", strategy="CRYPTO_SETTLEMENT_ENGINE", model_sha=SHA,
        record_id="order-record-" + market, recorded_ts_ms=BASE + 1,
        opportunity_id=replay, candidate_id=replay, order_id="order-" + market,
        market_id=market, token_id="token-" + market,
        decision_ts_ms=BASE, exchange_ts_ms=BASE, receive_ts_ms=BASE,
        book_snapshot_id="book-" + market, side="BUY",
        intended_action="TAKE", intended_size=5.0, limit_price=.5,
        metadata=common_md,
    )
    fill = LedgerEvent(
        event_type="FILL", strategy="CRYPTO_SETTLEMENT_ENGINE", model_sha=SHA,
        record_id="fill-record-" + market, recorded_ts_ms=BASE + 2,
        opportunity_id=replay, candidate_id=replay, order_id="order-" + market,
        fill_id="fill-" + market, position_id="position-" + market,
        market_id=market, token_id="token-" + market,
        decision_ts_ms=BASE, exchange_ts_ms=BASE, receive_ts_ms=BASE,
        book_snapshot_id="book-" + market, side="BUY",
        fill_price=.5, filled_size=5.0, complete=True,
        fee=.01, fee_source="SYNTHETIC", metadata=common_md,
    )
    events = [reserve, order, fill]
    if final:
        events.append(LedgerEvent(
            event_type="FINAL", strategy="CRYPTO_SETTLEMENT_ENGINE", model_sha=SHA,
            record_id="final-record-" + market, recorded_ts_ms=BASE + 100,
            opportunity_id=replay, candidate_id=replay, order_id="order-" + market,
            fill_id="fill-" + market, position_id="position-" + market,
            market_id=market, token_id="token-" + market, side="BUY",
            final_pnl=pnl, realized_cashflow=max(0.0, pnl + 2.51),
            metadata=common_md,
        ))
    return manifest, events


class MultiCryptoForwardReportTest(unittest.TestCase):
    def test_complete_cohort_uses_authorized_market_denominator(self) -> None:
        manifest, events = cohort_events(pnl=1.25)
        report = summarize(events, manifest, bootstrap_draws=100)
        self.assertEqual(report["status"], "COMPLETE_ACCOUNTING")
        self.assertEqual(report["authorized_markets"], 1)
        self.assertEqual(report["filled_markets"], 1)
        self.assertEqual(report["resolved_markets"], 1)
        self.assertEqual(report["total_pnl"], 1.25)
        self.assertEqual(report["annualized_sharpe"], None)
        self.assertFalse(report["minimum_market_target_met"])
        self.assertFalse(report["entry_authority"])

    def test_pending_final_is_missing_not_zero(self) -> None:
        manifest, events = cohort_events(final=False)
        report = summarize(events, manifest, bootstrap_draws=100)
        self.assertEqual(report["status"], "IN_PROGRESS")
        self.assertEqual(report["pending_markets"], 1)
        self.assertIsNone(report["total_pnl"])
        self.assertEqual(report["resolved_pnl_contribution"], 0)
        self.assertEqual(report["economic_evidence"], "PENDING")

    def test_duplicate_final_and_packet_tampering_fail_closed(self) -> None:
        manifest, events = cohort_events()
        events.append(replace(events[-1], record_id="duplicate-final"))
        with self.assertRaisesRegex(ReplayError, "DUPLICATE_FINAL"):
            summarize(events, manifest)
        manifest, events = cohort_events()
        metadata = dict(events[1].metadata)
        metadata["multi_crypto_forward"] = dict(metadata["multi_crypto_forward"], protocol_hash="9" * 64)
        events[1] = replace(events[1], metadata=metadata)
        with self.assertRaisesRegex(ReplayError, "LINEAGE_CONFLICT"):
            summarize(events, manifest)

    def test_multiple_authorized_markets_keep_concentration_diagnostics(self) -> None:
        manifest, first = cohort_events(pnl=2.0, market="m1", shock="macro")
        _, second = cohort_events(pnl=-1.0, market="m2", shock="macro")
        report = summarize(first + second, manifest, bootstrap_draws=100)
        self.assertEqual(report["authorized_markets"], 2)
        self.assertEqual(report["total_pnl"], 1.0)
        self.assertEqual(report["largest_positive_market_fraction_of_gross_positive_pnl"], 1.0)
        self.assertEqual(report["pnl_without_best_resolved_market"], -1.0)
        self.assertEqual(report["cluster_pnl_ci"]["status"], "INSUFFICIENT_EVIDENCE")


if __name__ == "__main__":
    unittest.main()
