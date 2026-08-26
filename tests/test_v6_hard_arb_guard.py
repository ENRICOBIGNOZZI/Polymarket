from __future__ import annotations

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))

from v6_hard_arb_guard import book_freshness, exchange_book_freshness, normalize_timestamp_ms


class HardArbFreshnessGuardTest(unittest.TestCase):
    def test_accepts_fresh_low_skew_receive_times(self):
        live = {"a": {"received_ms": 10_000}, "b": {"received_ms": 10_040}}
        ok, reason, age, skew = book_freshness(
            live,
            ["a", "b"],
            now_ms=10_100,
            max_leg_age_ms=200,
            max_cross_leg_skew_ms=100,
        )
        self.assertTrue(ok)
        self.assertEqual(reason, "ok")
        self.assertEqual(age, 100)
        self.assertEqual(skew, 40)

    def test_rejects_stale_receive_times(self):
        live = {"a": {"received_ms": 10_000}, "b": {"received_ms": 10_040}}
        ok, reason, _, _ = book_freshness(
            live,
            ["a", "b"],
            now_ms=10_500,
            max_leg_age_ms=200,
            max_cross_leg_skew_ms=100,
        )
        self.assertFalse(ok)
        self.assertEqual(reason, "max_leg_age")

    def test_rejects_cross_leg_receive_skew(self):
        live = {"a": {"received_ms": 10_000}, "b": {"received_ms": 10_300}}
        ok, reason, _, _ = book_freshness(
            live,
            ["a", "b"],
            now_ms=10_320,
            max_leg_age_ms=500,
            max_cross_leg_skew_ms=100,
        )
        self.assertFalse(ok)
        self.assertEqual(reason, "cross_leg_skew")

    def test_rejects_missing_receive_timestamp(self):
        live = {"a": {}, "b": {"received_ms": 10_040}}
        ok, reason, _, _ = book_freshness(
            live,
            ["a", "b"],
            now_ms=10_100,
            max_leg_age_ms=200,
            max_cross_leg_skew_ms=100,
        )
        self.assertFalse(ok)
        self.assertEqual(reason, "missing_receive_timestamp")

    def test_accepts_fresh_low_skew_exchange_snapshots(self):
        live = {"a": {"exchange_ts_ms": 9_990}, "b": {"exchange_ts_ms": 10_010}}
        ok, reason, age, skew = exchange_book_freshness(
            live,
            ["a", "b"],
            now_ms=10_100,
            max_snapshot_age_ms=200,
            max_snapshot_skew_ms=100,
        )
        self.assertTrue(ok)
        self.assertEqual(reason, "ok")
        self.assertEqual(age, 110)
        self.assertEqual(skew, 20)

    def test_rejects_stale_exchange_snapshot_even_if_locally_fresh(self):
        live = {
            "a": {"received_ms": 10_095, "exchange_ts_ms": 9_000},
            "b": {"received_ms": 10_096, "exchange_ts_ms": 9_010},
        }
        receive_ok, _, _, _ = book_freshness(
            live,
            ["a", "b"],
            now_ms=10_100,
            max_leg_age_ms=200,
            max_cross_leg_skew_ms=100,
        )
        exchange_ok, reason, _, _ = exchange_book_freshness(
            live,
            ["a", "b"],
            now_ms=10_100,
            max_snapshot_age_ms=500,
            max_snapshot_skew_ms=100,
        )
        self.assertTrue(receive_ok)
        self.assertFalse(exchange_ok)
        self.assertEqual(reason, "max_exchange_snapshot_age")

    def test_rejects_exchange_snapshot_skew_hidden_by_common_receive_time(self):
        live = {
            "a": {"received_ms": 10_095, "exchange_ts_ms": 9_900},
            "b": {"received_ms": 10_095, "exchange_ts_ms": 10_050},
        }
        receive_ok, _, _, receive_skew = book_freshness(
            live,
            ["a", "b"],
            now_ms=10_100,
            max_leg_age_ms=200,
            max_cross_leg_skew_ms=100,
        )
        exchange_ok, reason, _, exchange_skew = exchange_book_freshness(
            live,
            ["a", "b"],
            now_ms=10_100,
            max_snapshot_age_ms=500,
            max_snapshot_skew_ms=100,
        )
        self.assertTrue(receive_ok)
        self.assertEqual(receive_skew, 0)
        self.assertFalse(exchange_ok)
        self.assertEqual(reason, "exchange_snapshot_skew")
        self.assertEqual(exchange_skew, 150)

    def test_rejects_missing_exchange_timestamp(self):
        live = {"a": {}, "b": {"exchange_ts_ms": 10_040}}
        ok, reason, _, _ = exchange_book_freshness(
            live,
            ["a", "b"],
            now_ms=10_100,
            max_snapshot_age_ms=200,
            max_snapshot_skew_ms=100,
        )
        self.assertFalse(ok)
        self.assertEqual(reason, "missing_exchange_timestamp")

    def test_normalizes_seconds_milliseconds_and_microseconds(self):
        self.assertEqual(normalize_timestamp_ms(1_787_700_000), 1_787_700_000_000)
        self.assertEqual(normalize_timestamp_ms(1_787_700_000_123), 1_787_700_000_123)
        self.assertEqual(normalize_timestamp_ms(1_787_700_000_123_000), 1_787_700_000_123)


if __name__ == "__main__":
    unittest.main()
