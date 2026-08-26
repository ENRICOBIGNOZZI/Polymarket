from __future__ import annotations

import random
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = ROOT / "scripts"
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))

import v7_cross_sectional_rank_core as core
import v7_cross_sectional_rank_frozen as frozen
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


class FrozenRankingHoldoutTest(unittest.TestCase):
    @staticmethod
    def _rows(start_ts: int, sections: int, bucket_seconds: int) -> list[core.TrainingRow]:
        rows: list[core.TrainingRow] = []
        for section in range(sections):
            ts = start_ts + section * bucket_seconds
            for market in range(10):
                scale = 0.01 * (market + 1)
                features = tuple(
                    scale * (feature + 1) + 0.0001 * section
                    for feature in range(len(core.FEATURE_NAMES))
                )
                target = 0.2 * features[0] - 0.1 * features[1] + 0.00001 * section
                rows.append(
                    core.TrainingRow(
                        ts=ts,
                        label_ts=ts + 2 * bucket_seconds,
                        market_id=f"m{market}",
                        event_id=f"e{market}",
                        group="g",
                        probability=0.5,
                        features=features,
                        target_logit=target,
                    )
                )
        return rows

    def test_frozen_fit_never_absorbs_holdout_rows(self) -> None:
        bucket = 1800
        holdout = UTC_DAY_BASE + 80 * bucket
        rows = self._rows(holdout - 70 * bucket, 90, bucket)
        metrics, fit = frozen.frozen_section_metrics(
            rows,
            holdout_start_ts=holdout,
            bucket_seconds=bucket,
            window_seconds=21 * 86400,
            embargo_steps=1,
            ridge=0.05,
            half_life_seconds=7 * 86400,
            min_train_rows=100,
            min_train_cross_sections=20,
            tail_fraction=0.2,
        )
        self.assertIsNotNone(fit)
        assert fit is not None
        cutoff = frozen.frozen_training_label_cutoff_ts(
            holdout,
            bucket_seconds=bucket,
            embargo_steps=1,
        )
        self.assertLessEqual(fit.train_end_ts, cutoff)
        self.assertTrue(metrics)
        self.assertTrue(all(int(row["ts"]) >= holdout for row in metrics))

        later_rows = rows + self._rows(holdout + 30 * bucket, 20, bucket)
        later_metrics, later_fit = frozen.frozen_section_metrics(
            later_rows,
            holdout_start_ts=holdout,
            bucket_seconds=bucket,
            window_seconds=21 * 86400,
            embargo_steps=1,
            ridge=0.05,
            half_life_seconds=7 * 86400,
            min_train_rows=100,
            min_train_cross_sections=20,
            tail_fraction=0.2,
        )
        self.assertIsNotNone(later_fit)
        assert later_fit is not None
        self.assertEqual(fit.beta, later_fit.beta)
        self.assertEqual(fit.train_start_ts, later_fit.train_start_ts)
        self.assertEqual(fit.train_end_ts, later_fit.train_end_ts)
        self.assertEqual(metrics, later_metrics[: len(metrics)])


if __name__ == "__main__":
    unittest.main()
