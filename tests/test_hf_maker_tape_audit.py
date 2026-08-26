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


if __name__ == "__main__":
    unittest.main()
