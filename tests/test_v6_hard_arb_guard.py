from __future__ import annotations

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))

from v6_hard_arb_guard import book_freshness


class HardArbFreshnessGuardTest(unittest.TestCase):
    def test_accepts_fresh_low_skew_books(self):
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

    def test_rejects_stale_books(self):
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

    def test_rejects_cross_leg_skew(self):
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


if __name__ == "__main__":
    unittest.main()
