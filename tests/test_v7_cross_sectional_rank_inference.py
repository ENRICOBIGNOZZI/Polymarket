from __future__ import annotations

import random
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = ROOT / "scripts"
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))

import audit_v7_ranking_frozen_holdout as holdout_audit
import v7_cross_sectional_rank_inference as inference

UTC_DAY_BASE = (1_700_000_000 // 86400) * 86400


class BlockedRankingInferenceTest(unittest.TestCase):
    def test_robust_positive_daily_process_passes(self) -> None:
        rng = random.Random(7)
        metrics = []
        base = UTC_DAY_BASE
        for day in range(30):
            for slot in range(8):
                metrics.append(
                    {
                        "ts": base + day * 86400 + slot * 1800,
                        "rank_ic": 0.04 + rng.uniform(-0.015, 0.015),
                        "top_bottom_logit_spread": 0.003 + rng.uniform(-0.001, 0.001),
                        "directional_hit_rate": 0.56,
                        "n": 100,
                    }
                )
        result = inference.blocked_inference(metrics, bootstrap_samples=999, seed=42)
        ok, reasons = inference.discovery_robustness_gate(result)
        self.assertTrue(ok, reasons)
        self.assertEqual(result["days"], 30)
        self.assertLessEqual(result["rank_ic_bootstrap_p_mean_nonpositive"], 0.05)
        self.assertLessEqual(result["top_bottom_bootstrap_p_mean_nonpositive"], 0.05)

    def test_concentrated_or_unstable_signal_fails(self) -> None:
        metrics = []
        base = UTC_DAY_BASE
        for day in range(30):
            sign = 1.0 if day < 15 else -1.0
            for slot in range(4):
                metrics.append(
                    {
                        "ts": base + day * 86400 + slot * 1800,
                        "rank_ic": sign * 0.05,
                        "top_bottom_logit_spread": sign * 0.004,
                        "directional_hit_rate": 0.5,
                        "n": 100,
                    }
                )
        result = inference.blocked_inference(metrics, bootstrap_samples=499, seed=9)
        ok, reasons = inference.discovery_robustness_gate(result)
        self.assertFalse(ok)
        self.assertTrue(
            "rank_ic_second_half_mean" in reasons
            or "top_bottom_second_half_mean" in reasons
        )

    def test_too_few_days_fails_even_if_positive(self) -> None:
        base = UTC_DAY_BASE
        metrics = [
            {
                "ts": base + day * 86400,
                "rank_ic": 0.1,
                "top_bottom_logit_spread": 0.01,
                "directional_hit_rate": 0.6,
                "n": 100,
            }
            for day in range(10)
        ]
        result = inference.blocked_inference(metrics, bootstrap_samples=199, seed=1)
        ok, reasons = inference.discovery_robustness_gate(result)
        self.assertFalse(ok)
        self.assertIn("insufficient_day_blocks", reasons)

    def test_frozen_holdout_audit_detects_advancing_training_cutoff(self) -> None:
        holdout_start = 1_000_000
        observed_score = holdout_start + 10 * 3600 + 30 * 60
        two_hour = holdout_audit.audit_horizon(
            horizon_minutes=120,
            holdout_start_ts=holdout_start,
            observed_score_ts=observed_score,
            fidelity_minutes=30,
            embargo_buckets=1,
        )
        six_hour = holdout_audit.audit_horizon(
            horizon_minutes=360,
            holdout_start_ts=holdout_start,
            observed_score_ts=observed_score,
            fidelity_minutes=30,
            embargo_buckets=1,
        )
        self.assertTrue(two_hour.holdout_training_can_enter_by_observed_score)
        self.assertTrue(six_hour.holdout_training_can_enter_by_observed_score)
        self.assertEqual(two_hour.frozen_max_training_origin_ts, holdout_start - 9000)
        self.assertEqual(six_hour.frozen_max_training_origin_ts, holdout_start - 23400)
        self.assertGreater(two_hour.rolling_max_training_origin_at_observed_score_ts, holdout_start)
        self.assertGreater(six_hour.rolling_max_training_origin_at_observed_score_ts, holdout_start)


if __name__ == "__main__":
    unittest.main()
