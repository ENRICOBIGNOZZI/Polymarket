from __future__ import annotations

import sys
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = ROOT / "scripts"
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))

import v7_micro_taker_data as data
import v7_micro_taker_worker as worker


NOW = 1_800_000_000


def raw_book(token: str, timestamp: object) -> dict[str, object]:
    return {
        "asset_id": token,
        "timestamp": timestamp,
        "tick_size": "0.01",
        "min_order_size": "1",
        "bids": [{"price": "0.49", "size": "100"}],
        "asks": [{"price": "0.51", "size": "100"}],
    }


class MicroTakerBookFreshnessTest(unittest.TestCase):
    def test_epoch_seconds_normalizes_clob_second_and_millisecond_timestamps(self) -> None:
        self.assertEqual(data.epoch_seconds(str(NOW)), NOW)
        self.assertEqual(data.epoch_seconds(str(NOW * 1000 + 321)), NOW)
        self.assertEqual(data.epoch_seconds(str(NOW * 1_000_000 + 321_000)), NOW)
        self.assertEqual(data.epoch_seconds("not-a-timestamp"), 0)

    def test_fetch_books_preserves_exchange_and_local_receive_times(self) -> None:
        market = SimpleNamespace(yes="yes-token", no="no-token")
        rows = [raw_book("yes-token", str((NOW - 2) * 1000)), raw_book("no-token", str((NOW - 1) * 1000))]
        with mock.patch.object(data, "request_json", return_value=rows), mock.patch.object(data.time, "time", return_value=NOW):
            books = data.fetch_books("https://clob.invalid", [market])
        self.assertEqual(books["yes-token"].exchange_ts, NOW - 2)
        self.assertEqual(books["yes-token"].received_ts, NOW)
        self.assertEqual(books["no-token"].exchange_ts, NOW - 1)
        self.assertEqual(books["no-token"].received_ts, NOW)

    def test_fresh_pair_is_admitted_with_oldest_causal_timestamp(self) -> None:
        yes = data.Book(raw_book("yes", str((NOW - 2) * 1000)), received_ts=NOW)
        no = data.Book(raw_book("no", str((NOW - 1) * 1000)), received_ts=NOW)
        snapshot = worker.book_snapshot(yes, no, liquidity=1000.0, now=NOW, max_age_seconds=5)
        self.assertIsNotNone(snapshot)
        assert snapshot is not None
        self.assertEqual(snapshot.received_ts, NOW - 2)

    def test_stale_exchange_snapshot_is_not_rejuvenated_by_decision_time(self) -> None:
        yes = data.Book(raw_book("yes", str((NOW - 8) * 1000)), received_ts=NOW)
        no = data.Book(raw_book("no", str((NOW - 1) * 1000)), received_ts=NOW)
        self.assertIsNone(worker.book_snapshot(yes, no, liquidity=1000.0, now=NOW, max_age_seconds=5))

    def test_missing_exchange_timestamp_fails_closed(self) -> None:
        yes = data.Book(raw_book("yes", ""), received_ts=NOW)
        no = data.Book(raw_book("no", str(NOW * 1000)), received_ts=NOW)
        self.assertIsNone(worker.book_snapshot(yes, no, liquidity=1000.0, now=NOW, max_age_seconds=5))

    def test_future_exchange_or_receive_timestamp_fails_closed(self) -> None:
        future_exchange = data.Book(raw_book("yes", str((NOW + 2) * 1000)), received_ts=NOW)
        normal = data.Book(raw_book("no", str(NOW * 1000)), received_ts=NOW)
        self.assertIsNone(worker.book_snapshot(future_exchange, normal, liquidity=1000.0, now=NOW, max_age_seconds=5))
        future_receive = data.Book(raw_book("yes", str(NOW * 1000)), received_ts=NOW + 2)
        self.assertIsNone(worker.book_snapshot(future_receive, normal, liquidity=1000.0, now=NOW, max_age_seconds=5))


if __name__ == "__main__":
    unittest.main()
