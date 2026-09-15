from __future__ import annotations

import json
from decimal import Decimal
from pathlib import Path
import sys
import unittest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))
import v7_public_book_wire_probe as probe


class PublicBookWireProbeTest(unittest.TestCase):
    def test_book_diagnostic_distinguishes_timestamp_tick_and_cross(self) -> None:
        outer = {"event_type": "book", "timestamp": 123, "asset_id": "t"}
        book = {
            "event_type": "book", "timestamp": 123, "asset_id": "t",
            "bids": [{"price": "0.495", "size": "3"}],
            "asks": [{"price": "0.505", "size": "4"}],
        }
        row = probe.diagnose_book(outer, book, Decimal("0.001"))
        self.assertEqual(row["outer_timestamp"], 123)
        self.assertEqual(row["payload_timestamp"], 123)
        self.assertTrue(row["all_positive_levels_tick_aligned"])
        self.assertFalse(row["crossed_or_locked"])
        self.assertEqual(row["best_bid"], "0.495")
        self.assertEqual(row["best_ask"], "0.505")

        row = probe.diagnose_book(outer, book, Decimal("0.01"))
        self.assertFalse(row["all_positive_levels_tick_aligned"])

        crossed = dict(book)
        crossed["asks"] = [{"price": "0.490", "size": "4"}]
        self.assertTrue(probe.diagnose_book(outer, crossed, Decimal("0.001"))["crossed_or_locked"])

    def test_extracts_wrapped_and_array_book_events(self) -> None:
        wrapped = {
            "type": "book",
            "payload": {"asset_id": "a", "timestamp": 1, "bids": [], "asks": []},
        }
        classic = {"event_type": "book", "asset_id": "b", "timestamp": 2, "bids": [], "asks": []}
        rows = list(probe._events([wrapped, classic]))
        self.assertEqual(len(rows), 2)
        self.assertEqual(rows[0][1]["asset_id"], "a")
        self.assertEqual(rows[1][1]["asset_id"], "b")

    def test_client_frames_are_masked_and_payload_roundtrips(self) -> None:
        payload = json.dumps({"type": "market"}).encode()
        frame = probe._client_frame(1, payload)
        self.assertTrue(frame[1] & 0x80)
        length = frame[1] & 0x7F
        self.assertEqual(length, len(payload))
        mask = frame[2:6]
        encoded = frame[6:]
        decoded = bytes(byte ^ mask[i & 3] for i, byte in enumerate(encoded))
        self.assertEqual(decoded, payload)

    def test_probe_has_no_order_or_auth_surface(self) -> None:
        source = (ROOT / "scripts/v7_public_book_wire_probe.py").read_text()
        self.assertIn("ZERO_AUTHORITY_PUBLIC_DIAGNOSTIC", source)
        self.assertNotIn("private_key", source.lower())
        self.assertNotIn("api_key", source.lower())
        self.assertNotIn("place_order", source.lower())
        self.assertNotIn("submit_order", source.lower())


if __name__ == "__main__":
    unittest.main()
