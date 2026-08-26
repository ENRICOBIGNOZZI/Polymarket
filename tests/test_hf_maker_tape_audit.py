from __future__ import annotations

import csv
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from scripts.hf_maker_tape_audit import summarize


class HFMakerTapeAuditTest(unittest.TestCase):
    def _write(self, path: Path, fields: list[str], rows: list[dict[str, object]]) -> None:
        with path.open("w", newline="", encoding="utf-8") as fh:
            writer = csv.DictWriter(fh, fieldnames=fields)
            writer.writeheader()
            writer.writerows(rows)

    def test_separates_causal_receive_from_delayed_event_time_fill(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            self._write(
                root / "orders.csv",
                ["timestamp", "action", "market_id", "slug", "side", "token_id", "limit_price", "remaining_shares", "queue_ahead", "signal_edge", "confidence"],
                [
                    {"timestamp": 100, "action": "POST", "market_id": "m1", "slug": "x", "side": "YES", "token_id": "t1", "limit_price": 0.50, "remaining_shares": 10, "queue_ahead": 5, "signal_edge": 0.01, "confidence": 0.8},
                    {"timestamp": 160, "action": "CANCEL_TTL", "market_id": "m1", "slug": "x", "side": "YES", "token_id": "t1", "limit_price": 0.50, "remaining_shares": 10, "queue_ahead": 5, "signal_edge": 0, "confidence": 0},
                    {"timestamp": 200, "action": "POST", "market_id": "m2", "slug": "y", "side": "NO", "token_id": "t2", "limit_price": 0.40, "remaining_shares": 4, "queue_ahead": 2, "signal_edge": 0.01, "confidence": 0.8},
                    {"timestamp": 260, "action": "CANCEL_TTL", "market_id": "m2", "slug": "y", "side": "NO", "token_id": "t2", "limit_price": 0.40, "remaining_shares": 4, "queue_ahead": 2, "signal_edge": 0, "confidence": 0},
                ],
            )
            self._write(
                root / "tape.csv",
                ["timestamp", "received_ms", "lag_ms", "condition_id", "asset_id", "outcome", "side", "price", "size", "transaction_hash", "slug", "event_slug"],
                [
                    {"timestamp": 120, "received_ms": 121000, "lag_ms": 1000, "condition_id": "c1", "asset_id": "t1", "outcome": "Yes", "side": "SELL", "price": 0.49, "size": 8, "transaction_hash": "a", "slug": "x", "event_slug": "e"},
                    {"timestamp": 240, "received_ms": 270000, "lag_ms": 30000, "condition_id": "c2", "asset_id": "t2", "outcome": "No", "side": "SELL", "price": 0.39, "size": 10, "transaction_hash": "b", "slug": "y", "event_slug": "e"},
                    {"timestamp": 130, "received_ms": 131000, "lag_ms": 1000, "condition_id": "c1", "asset_id": "t1", "outcome": "Yes", "side": "BUY", "price": 0.49, "size": 100, "transaction_hash": "c", "slug": "x", "event_slug": "e"},
                ],
            )
            out = summarize(root / "orders.csv", root / "tape.csv", 60)
            self.assertEqual(out["schema"], "hf_maker_shared_tape_audit_v2")
            self.assertEqual(out["orders"], 2)
            self.assertAlmostEqual(out["posted_notional"], 6.6)
            self.assertEqual(out["causal_fill_orders"], 1)
            self.assertAlmostEqual(out["causal_fill_order_rate"], 0.5)
            self.assertAlmostEqual(out["causal_filled_shares"], 3.0)
            self.assertAlmostEqual(out["causal_filled_notional"], 1.5)
            self.assertEqual(out["event_time_fill_orders"], 2)
            self.assertAlmostEqual(out["event_time_fill_order_rate"], 1.0)
            self.assertAlmostEqual(out["event_time_filled_shares"], 7.0)
            self.assertAlmostEqual(out["event_time_filled_notional"], 3.1)
            self.assertEqual(out["delayed_only_fill_orders"], 1)
            self.assertEqual(out["candidate_rows_post_plus_queue_skip"], 2)
            self.assertEqual(out["candidate_unique_tokens"], 2)
            self.assertEqual(out["candidate_tokens_with_any_tape_trade"], 2)
            self.assertEqual(out["posted_tokens_with_any_tape_trade"], 2)

    def test_candidate_activity_includes_queue_skips_and_pre_entry_is_receive_time_causal(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            fields = ["timestamp", "action", "market_id", "slug", "side", "token_id", "limit_price", "remaining_shares", "queue_ahead", "signal_edge", "confidence"]
            self._write(
                root / "orders.csv",
                fields,
                [
                    {"timestamp": 100, "action": "POST", "market_id": "m1", "slug": "a", "side": "YES", "token_id": "t1", "limit_price": 0.50, "remaining_shares": 5, "queue_ahead": 5, "signal_edge": 0.01, "confidence": 0.8},
                    {"timestamp": 160, "action": "CANCEL_TTL", "market_id": "m1", "slug": "a", "side": "YES", "token_id": "t1", "limit_price": 0.50, "remaining_shares": 5, "queue_ahead": 5, "signal_edge": 0, "confidence": 0},
                    {"timestamp": 100, "action": "SKIP_QUEUE", "market_id": "m2", "slug": "b", "side": "YES", "token_id": "t2", "limit_price": 0.40, "remaining_shares": 5, "queue_ahead": 100, "signal_edge": 0.01, "confidence": 0.8},
                    {"timestamp": 200, "action": "POST", "market_id": "m3", "slug": "c", "side": "NO", "token_id": "t3", "limit_price": 0.30, "remaining_shares": 5, "queue_ahead": 5, "signal_edge": 0.01, "confidence": 0.8},
                    {"timestamp": 260, "action": "CANCEL_TTL", "market_id": "m3", "slug": "c", "side": "NO", "token_id": "t3", "limit_price": 0.30, "remaining_shares": 5, "queue_ahead": 5, "signal_edge": 0, "confidence": 0},
                ],
            )
            tape_fields = ["timestamp", "received_ms", "lag_ms", "condition_id", "asset_id", "outcome", "side", "price", "size", "transaction_hash", "slug", "event_slug"]
            self._write(
                root / "tape.csv",
                tape_fields,
                [
                    {"timestamp": 90, "received_ms": 99000, "lag_ms": 9000, "condition_id": "c1", "asset_id": "t1", "outcome": "Yes", "side": "BUY", "price": 0.51, "size": 2, "transaction_hash": "a", "slug": "a", "event_slug": "e"},
                    {"timestamp": 95, "received_ms": 101000, "lag_ms": 6000, "condition_id": "c1", "asset_id": "t1", "outcome": "Yes", "side": "BUY", "price": 0.51, "size": 2, "transaction_hash": "b", "slug": "a", "event_slug": "e"},
                    {"timestamp": 105, "received_ms": 106000, "lag_ms": 1000, "condition_id": "c2", "asset_id": "t2", "outcome": "Yes", "side": "BUY", "price": 0.41, "size": 2, "transaction_hash": "c", "slug": "b", "event_slug": "e"},
                    {"timestamp": 210, "received_ms": 211000, "lag_ms": 1000, "condition_id": "c4", "asset_id": "t4", "outcome": "Yes", "side": "SELL", "price": 0.20, "size": 2, "transaction_hash": "d", "slug": "d", "event_slug": "e"},
                ],
            )
            out = summarize(root / "orders.csv", root / "tape.csv", 60, 120)
            self.assertEqual(out["candidate_rows_post_plus_queue_skip"], 3)
            self.assertEqual(out["candidate_unique_markets"], 3)
            self.assertEqual(out["candidate_unique_tokens"], 3)
            self.assertEqual(out["candidate_tokens_with_any_tape_trade"], 2)
            self.assertAlmostEqual(out["candidate_token_activity_rate"], 2.0 / 3.0)
            self.assertEqual(out["posted_unique_tokens"], 2)
            self.assertEqual(out["posted_tokens_with_any_tape_trade"], 1)
            self.assertAlmostEqual(out["posted_token_activity_rate"], 0.5)
            self.assertEqual(out["posts_with_causal_pre_entry_trade"], 1)
            self.assertEqual(out["posts_with_any_causal_post_entry_trade"], 0)
            self.assertEqual(out["posts_with_eligible_post_entry_sell"], 0)


if __name__ == "__main__":
    unittest.main()
