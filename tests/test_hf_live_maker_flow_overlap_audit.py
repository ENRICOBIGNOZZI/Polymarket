#!/usr/bin/env python3
from __future__ import annotations

import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

from hf_live_maker_flow_overlap_audit import audit


def order(token: str = "A", created: int = 1000, price: float = 0.40) -> dict[str, str]:
    return {
        "market_id": "1",
        "slug": "market-a",
        "side": "YES",
        "token_id": token,
        "limit_price": str(price),
        "remaining_shares": "10",
        "queue_ahead": "20",
        "created_ts": str(created),
    }


def log(token: str = "A", ts: int = 1000, action: str = "POST") -> dict[str, str]:
    return {
        "timestamp": str(ts),
        "action": action,
        "market_id": "1",
        "slug": "market-a",
        "side": "YES",
        "token_id": token,
        "limit_price": "0.40",
        "remaining_shares": "10",
        "queue_ahead": "20",
        "signal_edge": "0.001",
        "confidence": "0.9",
    }


def trade(
    token: str = "A",
    event_ts: int = 995,
    received_ms: int = 995_000,
    side: str = "SELL",
    price: float = 0.39,
    size: float = 5,
) -> dict[str, str]:
    return {
        "timestamp": str(event_ts),
        "received_ms": str(received_ms),
        "asset_id": token,
        "side": side,
        "price": str(price),
        "size": str(size),
    }


class LiveMakerFlowOverlapAuditTest(unittest.TestCase):
    def test_nonempty_unrelated_tape_flags_static_activity_mismatch(self) -> None:
        report = audit([order("A")], [log("A")], [trade("B")])
        self.assertEqual(report["state"], "STATIC_MAKER_ACTIVITY_MISMATCH")
        self.assertEqual(report["resting"]["orders_with_any_tape_trade"], 0)
        self.assertEqual(report["resting"]["reserved_notional_usd"], 4.0)

    def test_received_after_decision_is_not_causal_activity(self) -> None:
        report = audit(
            [order("A")],
            [log("A")],
            [trade("A", event_ts=995, received_ms=1_001_000)],
        )
        self.assertEqual(report["state"], "STATIC_MAKER_ACTIVITY_MISMATCH")
        self.assertEqual(report["resting"]["orders_with_any_tape_trade"], 1)
        self.assertEqual(report["resting"]["orders_with_causal_recent_trade"], 0)

    def test_old_event_received_recently_is_not_recent_market_activity(self) -> None:
        report = audit(
            [order("A")],
            [log("A")],
            [trade("A", event_ts=50, received_ms=999_000)],
            lookback_seconds=900,
        )
        self.assertEqual(report["state"], "STATIC_MAKER_ACTIVITY_MISMATCH")
        self.assertEqual(report["resting"]["orders_with_causal_recent_trade"], 0)

    def test_compatible_sell_at_or_below_bid_counts(self) -> None:
        report = audit(
            [order("A", price=0.40)],
            [log("A")],
            [
                trade("A", side="SELL", price=0.40, size=3),
                trade("A", side="BUY", price=0.39, size=7),
                trade("A", side="SELL", price=0.41, size=11),
            ],
        )
        self.assertEqual(report["state"], "ACTIVITY_PRESENT")
        self.assertEqual(report["resting"]["orders_with_causal_recent_trade"], 1)
        self.assertEqual(report["resting"]["orders_with_compatible_sell"], 1)
        self.assertEqual(report["orders"][0]["compatible_sell_rows_pre_decision"], 1)
        self.assertEqual(report["orders"][0]["compatible_sell_volume_pre_decision"], 3.0)

    def test_empty_tape_is_inconclusive_not_zero_activity_evidence(self) -> None:
        report = audit([order("A")], [log("A")], [])
        self.assertEqual(report["state"], "INCONCLUSIVE_TAPE")
        self.assertFalse(report["tape"]["healthy_enough_for_overlap_audit"])

    def test_first_tick_signal_overlap_is_separate_from_resting_overlap(self) -> None:
        report = audit(
            [order("A")],
            [log("A"), log("B", action="SKIP_QUEUE")],
            [trade("B")],
        )
        self.assertEqual(report["first_tick"]["signal_tokens"], 2)
        self.assertEqual(report["first_tick"]["signal_tokens_with_any_tape_trade"], 1)
        self.assertEqual(report["resting"]["orders_with_any_tape_trade"], 0)


if __name__ == "__main__":
    unittest.main()
