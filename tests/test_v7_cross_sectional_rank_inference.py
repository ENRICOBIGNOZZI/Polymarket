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

    @staticmethod
    def _training_row(
        ts: int,
        label_ts: int,
        market_index: int,
        *,
        target_scale: float = 1.0,
        feature_scale: float = 1.0,
    ) -> core.TrainingRow:
        features = tuple(
            feature_scale
            * (
                0.05 * (market_index + 1)
                + 0.003 * (j + 1)
                + 0.000001 * (ts % 100_000) * ((j % 3) + 1)
            )
            for j in range(len(core.FEATURE_NAMES))
        )
        target = target_scale * (
            0.35 * features[0]
            - 0.12 * features[1]
            + 0.04 * (market_index - 2.5)
        )
        return core.TrainingRow(
            ts=ts,
            label_ts=label_ts,
            market_id=f"m{market_index}",
            event_id=f"e{market_index}",
            group="g",
            probability=0.4 + 0.02 * market_index,
            features=features,
            target_logit=target,
        )

    def test_frozen_fit_is_unchanged_by_holdout_origin_rows(self) -> None:
        holdout_start = 1_000_000
        embargo_seconds = 1800
        pre_rows = [
            self._training_row(ts, ts + 3600, market)
            for ts in (990_000, 992_000, 994_000)
            for market in range(6)
        ]
        holdout_rows = [
            self._training_row(
                ts,
                ts + 3600,
                market,
                target_scale=500.0,
                feature_scale=50.0,
            )
            for ts in (1_000_000, 1_001_800)
            for market in range(6)
        ]

        fit_pre = frozen.fit_at_holdout_boundary(
            pre_rows,
            holdout_start_ts=holdout_start,
            window_seconds=100_000,
            embargo_seconds=embargo_seconds,
            ridge=0.05,
            half_life_seconds=86_400,
            min_train_rows=12,
            min_train_cross_sections=2,
        )
        fit_contaminated_input = frozen.fit_at_holdout_boundary(
            pre_rows + holdout_rows,
            holdout_start_ts=holdout_start,
            window_seconds=100_000,
            embargo_seconds=embargo_seconds,
            ridge=0.05,
            half_life_seconds=86_400,
            min_train_rows=12,
            min_train_cross_sections=2,
        )

        self.assertIsNotNone(fit_pre)
        self.assertIsNotNone(fit_contaminated_input)
        assert fit_pre is not None and fit_contaminated_input is not None
        self.assertEqual(fit_pre.beta, fit_contaminated_input.beta)
        self.assertEqual(fit_pre.residual_sigma, fit_contaminated_input.residual_sigma)
        self.assertEqual(fit_pre.train_end_ts, fit_contaminated_input.train_end_ts)
        self.assertLessEqual(fit_pre.train_end_ts, holdout_start - embargo_seconds)

    def test_frozen_metrics_use_only_holdout_sections_with_fixed_fit(self) -> None:
        holdout_start = 1_000_000
        embargo_seconds = 1800
        pre_rows = [
            self._training_row(ts, ts + 3600, market)
            for ts in (990_000, 992_000, 994_000)
            for market in range(6)
        ]
        holdout_rows = [
            self._training_row(ts, ts + 3600, market)
            for ts in (1_000_000, 1_001_800)
            for market in range(6)
        ]
        fit, metrics = frozen.evaluate(
            pre_rows + holdout_rows,
            holdout_start_ts=holdout_start,
            window_seconds=100_000,
            embargo_seconds=embargo_seconds,
            ridge=0.05,
            half_life_seconds=86_400,
            min_train_rows=12,
            min_train_cross_sections=2,
            tail_fraction=0.2,
        )
        self.assertIsNotNone(fit)
        assert fit is not None
        self.assertLessEqual(fit.train_end_ts, holdout_start - embargo_seconds)
        self.assertEqual([row["ts"] for row in metrics], [1_000_000, 1_001_800])
        self.assertTrue(all(int(row["ts"]) >= holdout_start for row in metrics))


if __name__ == "__main__":
    unittest.main()
