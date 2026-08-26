#!/usr/bin/env python3
from __future__ import annotations

import importlib.util
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
MODULE_PATH = ROOT / "scripts" / "lf_v7_history_freshness_audit.py"
SPEC = importlib.util.spec_from_file_location("lf_v7_history_freshness_audit", MODULE_PATH)
assert SPEC and SPEC.loader
MODULE = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = MODULE
SPEC.loader.exec_module(MODULE)


class LocalFactorHistoryFreshnessTest(unittest.TestCase):
    def test_regular_but_stale_panel_fails_closed(self) -> None:
        now = 1_800_000_000
        bucket = 3600
        last = now - 12 * bucket
        times = [last - (59 - i) * bucket for i in range(60)]
        result = MODULE.assess_history_freshness(times, now, bucket, maximum_age_buckets=2)
        self.assertTrue(result.regular)
        self.assertFalse(result.fresh)
        self.assertEqual(result.reason, "stale_history_state")
        self.assertEqual(result.history_age_seconds, 12 * bucket)

    def test_recent_regular_panel_is_eligible(self) -> None:
        now = 1_800_000_000
        bucket = 1800
        last = now - bucket
        times = [last - (47 - i) * bucket for i in range(48)]
        result = MODULE.assess_history_freshness(times, now, bucket, maximum_age_buckets=2)
        self.assertTrue(result.regular)
        self.assertTrue(result.fresh)
        self.assertEqual(result.reason, "fresh_regular_history")

    def test_irregular_panel_is_not_made_fresh_by_recent_endpoint(self) -> None:
        now = 1_800_000_000
        bucket = 3600
        times = [now - 3 * bucket, now - bucket]
        result = MODULE.assess_history_freshness(times, now, bucket, maximum_age_buckets=2)
        self.assertFalse(result.regular)
        self.assertFalse(result.fresh)
        self.assertEqual(result.reason, "irregular_history")

    def test_stale_ar_state_can_materially_overstate_current_horizon_move(self) -> None:
        result = MODULE.stale_state_forecast_overstatement(phi=0.80, stale_bars=12, hold_bars=6)
        self.assertAlmostEqual(result["expected_current_residual_sd"], 2.0 * 0.8**12, places=12)
        self.assertGreater(result["absolute_forecast_overstatement_ratio"], 14.0)
        self.assertLess(result["current_origin_forecast_change_sd"], 0.0)

    def test_future_history_timestamp_fails_closed(self) -> None:
        now = 1_800_000_000
        bucket = 3600
        result = MODULE.assess_history_freshness([now, now + bucket], now, bucket, maximum_age_buckets=2)
        self.assertFalse(result.fresh)
        self.assertEqual(result.reason, "future_history_timestamp")


if __name__ == "__main__":
    unittest.main()
