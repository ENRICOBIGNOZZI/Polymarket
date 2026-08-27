#!/usr/bin/env python3
from __future__ import annotations

import importlib.util
import unittest
from pathlib import Path


SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "v7_fast_rest_snapshot_probe.py"
SPEC = importlib.util.spec_from_file_location("v7_fast_rest_snapshot_probe", SCRIPT)
MODULE = importlib.util.module_from_spec(SPEC)
assert SPEC and SPEC.loader
SPEC.loader.exec_module(MODULE)


class RestSnapshotProbeTest(unittest.TestCase):
    def test_timestamp_normalization_handles_seconds_ms_and_us(self):
        self.assertEqual(MODULE.normalize_timestamp_ms("1787790000"), 1787790000000)
        self.assertEqual(MODULE.normalize_timestamp_ms("1787790000123"), 1787790000123)
        self.assertEqual(MODULE.normalize_timestamp_ms("1787790000123000"), 1787790000123)
        self.assertEqual(MODULE.normalize_timestamp_ms("bad"), 0)

    def test_pair_requires_timestamp_hash_age_and_skew(self):
        left = {
            "exchange_ts_ms": 10_000,
            "received_ts_ms": 10_500,
            "snapshot_hash": "left",
        }
        right = {
            "exchange_ts_ms": 10_400,
            "received_ts_ms": 10_600,
            "snapshot_hash": "right",
        }
        ok, reason, metrics = MODULE.classify_pair(left, right, max_age_ms=5_000, max_skew_ms=1_500)
        self.assertTrue(ok)
        self.assertEqual(reason, "eligible")
        self.assertEqual(metrics["exchange_skew_ms"], 400)

        stale = dict(left, exchange_ts_ms=4_000)
        ok, reason, _ = MODULE.classify_pair(stale, right, max_age_ms=5_000, max_skew_ms=1_500)
        self.assertFalse(ok)
        self.assertEqual(reason, "stale_exchange_clock")

        skewed = dict(left, exchange_ts_ms=8_000)
        ok, reason, _ = MODULE.classify_pair(skewed, right, max_age_ms=5_000, max_skew_ms=1_500)
        self.assertFalse(ok)
        self.assertEqual(reason, "exchange_skew")

        missing_hash = dict(left, snapshot_hash="")
        ok, reason, _ = MODULE.classify_pair(missing_hash, right, max_age_ms=5_000, max_skew_ms=1_500)
        self.assertFalse(ok)
        self.assertEqual(reason, "missing_provenance")

    def test_summary_counts_strict_eligibility_without_inferring_continuity(self):
        markets = [
            {"market_id": "m1", "yes_token": "y1", "no_token": "n1"},
            {"market_id": "m2", "yes_token": "y2", "no_token": "n2"},
        ]
        books = {
            "y1": {"exchange_ts_ms": 10_000, "received_ts_ms": 10_500, "snapshot_hash": "a", "age_ms": 500},
            "n1": {"exchange_ts_ms": 10_200, "received_ts_ms": 10_500, "snapshot_hash": "b", "age_ms": 300},
            "y2": {"exchange_ts_ms": 2_000, "received_ts_ms": 10_500, "snapshot_hash": "c", "age_ms": 8_500},
            "n2": {"exchange_ts_ms": 10_300, "received_ts_ms": 10_500, "snapshot_hash": "d", "age_ms": 200},
        }
        summary = MODULE.summarize_round(markets, books, max_age_ms=5_000, max_skew_ms=1_500)
        self.assertEqual(summary["requested_tokens"], 4)
        self.assertEqual(summary["provenance_books"], 4)
        self.assertEqual(summary["strict_eligible_pairs"], 1)
        self.assertEqual(summary["pair_reject_reasons"]["eligible"], 1)
        self.assertEqual(summary["pair_reject_reasons"]["stale_exchange_clock"], 1)


if __name__ == "__main__":
    unittest.main()
