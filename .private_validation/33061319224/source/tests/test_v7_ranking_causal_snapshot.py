from __future__ import annotations

import time
import unittest

from scripts import v7_cross_sectional_rank as rank
from scripts import v7_cross_sectional_tail_relative_causal as causal


class RankingCausalSnapshotTest(unittest.TestCase):
    def setUp(self) -> None:
        causal._BOOKS_BY_TOKEN.clear()
        causal._BOOKS_BY_MARKET.clear()
        causal._PAIR_PROVENANCE.clear()
        causal._CURRENT_VALIDATION = None
        causal._GUARD_ATTEMPTS = 0
        causal._GUARD_REJECTIONS = 0

    @staticmethod
    def markets() -> list[rank.Market]:
        return [
            rank.Market("m1", "e1", "g", "y1", "n1", 100.0, {}),
            rank.Market("m2", "e2", "g", "y2", "n2", 100.0, {}),
        ]

    def test_full_current_cross_section_requires_source_clock_and_hash(self) -> None:
        now_ms = int(time.time() * 1000)

        def request_json(_url: str, payload):
            rows = []
            for request in payload:
                token = request["token_id"]
                rows.append({
                    "asset_id": token,
                    "timestamp": now_ms,
                    "hash": f"h-{token}",
                    "min_order_size": "1",
                    "bids": [{"price": "0.49", "size": "10"}],
                    "asks": [{"price": "0.51", "size": "10"}],
                })
            return rows

        old = causal.driver.base.request_json
        causal.driver.base.request_json = request_json
        try:
            books = causal._fetch_books("https://clob.invalid", self.markets())
        finally:
            causal.driver.base.request_json = old
        self.assertEqual(set(books), {"m1", "m2"})
        self.assertIsNotNone(causal._CURRENT_VALIDATION)
        self.assertTrue(causal._CURRENT_VALIDATION.ok)
        self.assertIsNotNone(causal._CURRENT_VALIDATION.snapshot_set_id)

    def test_missing_hash_fails_entire_current_cross_section_closed(self) -> None:
        now_ms = int(time.time() * 1000)

        def request_json(_url: str, payload):
            return [{
                "asset_id": request["token_id"],
                "timestamp": now_ms,
                "hash": "" if request["token_id"] == "n2" else f"h-{request['token_id']}",
                "bids": [{"price": "0.49", "size": "10"}],
                "asks": [{"price": "0.51", "size": "10"}],
            } for request in payload]

        old = causal.driver.base.request_json
        causal.driver.base.request_json = request_json
        try:
            books = causal._fetch_books("https://clob.invalid", self.markets())
        finally:
            causal.driver.base.request_json = old
        self.assertEqual(books, {})
        self.assertEqual(causal._GUARD_REJECTIONS, 1)
        self.assertFalse(causal._CURRENT_VALIDATION.ok)

    def test_pair_provenance_uses_actual_side_books(self) -> None:
        self.test_full_current_cross_section_requires_source_clock_and_hash()
        candidate = type("Candidate", (), {
            "top_market_id": "m1",
            "bottom_market_id": "m2",
            "horizon_seconds": 7200,
        })()
        provenance = causal._pair_snapshot(candidate)
        self.assertIsNotNone(provenance)
        self.assertGreater(provenance["exchange_ts_ms"], 0)
        self.assertGreaterEqual(provenance["receive_ts_ms"], provenance["exchange_ts_ms"])
        self.assertGreaterEqual(provenance["decision_ts_ms"], provenance["receive_ts_ms"])
        self.assertTrue(provenance["book_snapshot_id"])


if __name__ == "__main__":
    unittest.main()
