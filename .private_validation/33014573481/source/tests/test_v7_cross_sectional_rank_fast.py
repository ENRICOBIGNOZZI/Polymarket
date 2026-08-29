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
import v7_cross_sectional_rank_fast as fast


class RollingRidgeEquivalenceTest(unittest.TestCase):
    def rows(self) -> list[core.TrainingRow]:
        rng = random.Random(20260826)
        out: list[core.TrainingRow] = []
        bucket = 1800
        horizon_steps = 2
        for section in range(80):
            ts = 1_700_000_000 + section * bucket
            for market in range(12):
                features = tuple(rng.uniform(-2.0, 2.0) for _ in core.FEATURE_NAMES)
                target = (
                    0.020 * features[0]
                    - 0.015 * features[3]
                    + 0.007 * features[6]
                    + rng.uniform(-0.01, 0.01)
                )
                out.append(
                    core.TrainingRow(
                        ts=ts,
                        label_ts=ts + horizon_steps * bucket,
                        market_id=f"m{market}",
                        event_id=f"e{market // 2}",
                        group="g" + str(market % 3),
                        probability=0.5,
                        features=features,
                        target_logit=target,
                    )
                )
        return out

    def test_window_beta_matches_canonical_fit(self) -> None:
        rows = self.rows()
        bucket = 1800
        horizon_steps = 2
        by_ts: dict[int, list[core.TrainingRow]] = {}
        for row in rows:
            by_ts.setdefault(row.ts, []).append(row)
        section_times = sorted(by_ts)
        origin = max(row.label_ts for row in rows)
        moments = [
            fast._section_moments(
                by_ts[ts],
                origin_label_ts=origin,
                half_life_seconds=7 * 86400,
            )
            for ts in section_times
        ]
        p = len(core.FEATURE_NAMES)
        xtx = [[0.0] * p for _ in range(p)]
        xty = [0.0] * p
        wsum = 0.0
        nrows = 0
        left = 0
        right = 0
        for eval_ts in section_times:
            lower = eval_ts - 21 * 86400
            upper = eval_ts - bucket - horizon_steps * bucket
            while right < len(section_times) and section_times[right] <= upper:
                item = moments[right]
                fast._add_moments(xtx, xty, item, +1.0)
                wsum += item.weight_sum
                nrows += item.n_rows
                right += 1
            while left < right and section_times[left] < lower:
                item = moments[left]
                fast._add_moments(xtx, xty, item, -1.0)
                wsum -= item.weight_sum
                nrows -= item.n_rows
                left += 1
            if nrows < 100 or right - left < 20:
                continue
            canonical = core.fit_ridge(
                rows,
                asof_ts=eval_ts,
                window_seconds=21 * 86400,
                embargo_seconds=bucket,
                ridge=0.05,
                half_life_seconds=7 * 86400,
                min_rows=100,
                min_cross_sections=20,
            )
            self.assertIsNotNone(canonical)
            assert canonical is not None
            beta = fast._beta_from_window(xtx, xty, wsum, 0.05)
            for actual, expected in zip(beta, canonical.beta):
                self.assertAlmostEqual(actual, expected, places=10)

    def test_full_report_matches_canonical_evaluator(self) -> None:
        rows = self.rows()
        kwargs = dict(
            bucket_seconds=1800,
            horizon_steps=2,
            window_seconds=21 * 86400,
            embargo_steps=1,
            ridge=0.05,
            half_life_seconds=7 * 86400,
            min_train_rows=100,
            min_train_cross_sections=20,
            tail_fraction=0.2,
        )
        canonical = core.walk_forward_evaluate(rows, **kwargs)
        rolling = fast.walk_forward_evaluate(rows, **kwargs)
        self.assertEqual(rolling["cross_sections"], canonical["cross_sections"])
        self.assertEqual(rolling["predictions"], canonical["predictions"])
        for key in (
            "mean_rank_ic",
            "median_rank_ic",
            "positive_ic_fraction",
            "mean_top_bottom_logit_spread",
            "median_top_bottom_logit_spread",
            "directional_hit_rate",
            "mean_turnover",
            "decile_monotonicity",
        ):
            self.assertAlmostEqual(float(rolling[key]), float(canonical[key]), places=10, msg=key)
        for actual, expected in zip(rolling["decile_target_means"], canonical["decile_target_means"]):
            self.assertAlmostEqual(float(actual), float(expected), places=10)


if __name__ == "__main__":
    unittest.main()
