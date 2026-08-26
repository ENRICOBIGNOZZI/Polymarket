from __future__ import annotations

import csv
import tempfile
import unittest
from pathlib import Path

from scripts.hf_maker_ab_summary import summarize_arm
from scripts.hf_maker_tape_audit import summarize as summarize_tape


class HFMakerABSummaryTest(unittest.TestCase):
    def _write_csv(self, path: Path, fieldnames: list[str], rows: list[dict[str, object]]) -> None:
        with path.open("w", newline="", encoding="utf-8") as fh:
            writer = csv.DictWriter(fh, fieldnames=fieldnames)
            writer.writeheader()
            writer.writerows(rows)

    def test_realized_fill_conditioned_pnl_and_capital_time(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            self._write_csv(
                root / "maker_fills.csv",
                ["timestamp", "market_id", "slug", "action", "side", "shares", "price", "fee", "reason"],
                [
                    {"timestamp": 10, "market_id": "m1", "slug": "x", "action": "BUY_MAKER_PARTIAL", "side": "YES", "shares": 4, "price": 0.40, "fee": 0, "reason": "fill"},
                    {"timestamp": 11, "market_id": "m1", "slug": "x", "action": "BUY_MAKER", "side": "YES", "shares": 6, "price": 0.50, "fee": 0, "reason": "fill"},
                    {"timestamp": 70, "market_id": "m1", "slug": "x", "action": "SELL_TAKER_PARTIAL", "side": "YES", "shares": 5, "price": 0.60, "fee": 0.10, "reason": "max_hold"},
                ],
            )
            self._write_csv(
                root / "maker_order_log.csv",
                ["timestamp", "action", "market_id", "slug", "side", "token_id", "limit_price", "remaining_shares", "queue_ahead", "signal_edge", "confidence"],
                [
                    {"timestamp": 1, "action": "POST", "market_id": "m1", "slug": "x", "side": "YES", "token_id": "t", "limit_price": 0.4, "remaining_shares": 10, "queue_ahead": 5, "signal_edge": 0.01, "confidence": 0.8},
                    {"timestamp": 20, "action": "QUEUE_TRADE_DEPLETION", "market_id": "m1", "slug": "x", "side": "YES", "token_id": "t", "limit_price": 0.4, "remaining_shares": 10, "queue_ahead": 2, "signal_edge": 0, "confidence": 0},
                ],
            )
            self._write_csv(
                root / "maker_equity.csv",
                ["timestamp", "cash", "equity", "reserved_cash", "resting_orders", "positions", "peak_equity", "drawdown", "killed"],
                [
                    {"timestamp": 0, "cash": 100, "equity": 100, "reserved_cash": 20, "resting_orders": 1, "positions": 0, "peak_equity": 100, "drawdown": 0, "killed": 0},
                    {"timestamp": 10, "cash": 100, "equity": 100, "reserved_cash": 10, "resting_orders": 1, "positions": 0, "peak_equity": 100, "drawdown": 0, "killed": 0},
                    {"timestamp": 20, "cash": 100, "equity": 100, "reserved_cash": 0, "resting_orders": 0, "positions": 1, "peak_equity": 100, "drawdown": 0, "killed": 0},
                ],
            )

            out = summarize_arm("fixture", root)
            self.assertEqual(out["maker_fill_events"], 2)
            self.assertAlmostEqual(out["maker_filled_shares"], 10.0)
            self.assertAlmostEqual(out["closed_shares"], 5.0)
            self.assertAlmostEqual(out["open_shares"], 5.0)
            self.assertAlmostEqual(out["realized_fill_conditioned_pnl"], 0.60)
            self.assertAlmostEqual(out["realized_pnl_per_closed_share"], 0.12)
            self.assertAlmostEqual(out["reserved_capital_seconds"], 300.0)
            self.assertEqual(out["order_actions"]["POST"], 1)
            self.assertEqual(out["order_actions"]["QUEUE_TRADE_DEPLETION"], 1)

    def test_shared_tape_audit_separates_causal_and_delayed_indexing(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            self._write_csv(
                root / "orders.csv",
                ["timestamp", "action", "market_id", "slug", "side", "token_id", "limit_price", "remaining_shares", "queue_ahead", "signal_edge", "confidence"],
                [
                    {"timestamp": 100, "action": "POST", "market_id": "m1", "slug": "x", "side": "YES", "token_id": "t1", "limit_price": 0.50, "remaining_shares": 10, "queue_ahead": 5, "signal_edge": 0.01, "confidence": 0.8},
                    {"timestamp": 160, "action": "CANCEL_TTL", "market_id": "m1", "slug": "x", "side": "YES", "token_id": "t1", "limit_price": 0.50, "remaining_shares": 10, "queue_ahead": 5, "signal_edge": 0, "confidence": 0},
                    {"timestamp": 200, "action": "POST", "market_id": "m2", "slug": "y", "side": "NO", "token_id": "t2", "limit_price": 0.40, "remaining_shares": 4, "queue_ahead": 2, "signal_edge": 0.01, "confidence": 0.8},
                    {"timestamp": 260, "action": "CANCEL_TTL", "market_id": "m2", "slug": "y", "side": "NO", "token_id": "t2", "limit_price": 0.40, "remaining_shares": 4, "queue_ahead": 2, "signal_edge": 0, "confidence": 0},
                ],
            )
            self._write_csv(
                root / "tape.csv",
                ["timestamp", "received_ms", "lag_ms", "condition_id", "asset_id", "outcome", "side", "price", "size", "transaction_hash", "slug", "event_slug"],
                [
                    {"timestamp": 120, "received_ms": 121000, "lag_ms": 1000, "condition_id": "c1", "asset_id": "t1", "outcome": "Yes", "side": "SELL", "price": 0.49, "size": 8, "transaction_hash": "a", "slug": "x", "event_slug": "e"},
                    {"timestamp": 240, "received_ms": 270000, "lag_ms": 30000, "condition_id": "c2", "asset_id": "t2", "outcome": "No", "side": "SELL", "price": 0.39, "size": 10, "transaction_hash": "b", "slug": "y", "event_slug": "e"},
                    {"timestamp": 130, "received_ms": 131000, "lag_ms": 1000, "condition_id": "c1", "asset_id": "t1", "outcome": "Yes", "side": "BUY", "price": 0.49, "size": 100, "transaction_hash": "c", "slug": "x", "event_slug": "e"},
                ],
            )
            out = summarize_tape(root / "orders.csv", root / "tape.csv", 60)
            self.assertEqual(out["orders"], 2)
            self.assertEqual(out["causal_fill_orders"], 1)
            self.assertAlmostEqual(out["causal_filled_shares"], 3.0)
            self.assertEqual(out["event_time_fill_orders"], 2)
            self.assertAlmostEqual(out["event_time_filled_shares"], 7.0)
            self.assertEqual(out["delayed_only_fill_orders"], 1)


if __name__ == "__main__":
    unittest.main()
