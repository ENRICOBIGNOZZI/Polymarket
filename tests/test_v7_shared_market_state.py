from __future__ import annotations

import pathlib
import sys
import unittest

ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

from v7_shared_market_state import (  # noqa: E402
    SharedStateCursor, SharedStateError, synchronized_books, validate_payload,
)

SHA = "c" * 40


def payload(timestamp: int = 10_000, generation: int = 2) -> dict:
    def book(token: str, version: int) -> dict:
        return {
            "token_id": token, "market_id": "m", "condition_id": "c",
            "event_id": "e", "outcome": "YES", "exchange_ts_ms": 9_900,
            "receive_ts_ms": 9_950, "state_version": version,
            "lineage_epoch": 1, "lineage_continuous": True,
            "provenance": "WEBSOCKET", "tick_size": .01,
            "min_order_size": 1, "bids": [{"price": .4, "size": 10}],
            "asks": [{"price": .41, "size": 12}], "fee_verified": True,
            "fee_rate": 0, "fee_exponent": 1, "fee_taker_only": True,
        }
    return {
        "schema": "polymarket_v7_shared_market_state_v1", "timestamp_ms": timestamp,
        "snapshot_id": f"s-{generation}", "generation": generation,
        "producer": "FAST_STRUCTURAL_CPP_WEBSOCKET", "model_sha": SHA,
        "paper_only": True, "authenticated_execution": False,
        "real_order_submission": False, "books": [book("a", generation), book("b", generation)],
    }


class SharedMarketStateTests(unittest.TestCase):
    def test_cpp_fast_structural_is_the_single_atomic_producer(self) -> None:
        producer = (ROOT / "src" / "fast_runtime" / "part2.inc").read_text(encoding="utf-8")
        loop = (ROOT / "scripts" / "paper_v7_execution_loop.sh").read_text(encoding="utf-8")
        self.assertIn("polymarket_v7_shared_market_state_v1", producer)
        self.assertIn('"FAST_STRUCTURAL_CPP_WEBSOCKET"', producer)
        self.assertIn('market_data" / "shared_state.json', producer)
        self.assertIn('--shared-state "$RUN_ROOT/market_data/shared_state.json"', loop)

    def test_atomic_bundle_uses_publish_clock_not_quiet_book_age(self) -> None:
        state = validate_payload(payload(), expected_sha=SHA, now_ms=10_100)
        books = synchronized_books(state, ["a", "b"])
        self.assertEqual({row["bus_snapshot_id"] for row in books.values()}, {"s-2"})
        self.assertEqual(books["a"]["received_ms"], 10_000)
        self.assertEqual(books["a"]["source_receive_ts_ms"], 9_950)

    def test_stale_publish_and_broken_lineage_fail_closed(self) -> None:
        with self.assertRaises(SharedStateError):
            validate_payload(payload(), expected_sha=SHA, now_ms=20_000, max_publish_age_ms=100)
        raw = payload()
        raw["books"][1]["lineage_continuous"] = False
        state = validate_payload(raw, expected_sha=SHA, now_ms=10_100)
        with self.assertRaises(SharedStateError):
            synchronized_books(state, ["a", "b"])

    def test_cursor_rejects_sequence_regression(self) -> None:
        cursor = SharedStateCursor()
        cursor.accept(validate_payload(payload(generation=3), expected_sha=SHA, now_ms=10_100))
        with self.assertRaises(SharedStateError):
            cursor.accept(validate_payload(payload(generation=2), expected_sha=SHA, now_ms=10_100))


if __name__ == "__main__":
    unittest.main()
