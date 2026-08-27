from __future__ import annotations

import math
import sys
import time
import unittest
from dataclasses import replace
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SCRIPT_DIR = ROOT / "scripts"
if str(SCRIPT_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPT_DIR))

import v7_local_factor_core as lf
import v7_model_book_snapshot as books
import v7_pca_stat_arb_core as pca


class CausalBookSnapshotTests(unittest.TestCase):
    def _row(self, timestamp, *, token="t1", snapshot_hash="h1"):
        return {
            "asset_id": token,
            "timestamp": timestamp,
            "hash": snapshot_hash,
            "min_order_size": "1",
            "bids": [{"price": "0.49", "size": "10"}],
            "asks": [{"price": "0.51", "size": "12"}],
        }

    def test_timestamp_normalization_and_future_fail_closed(self):
        now_ms = 1_787_000_000_000
        self.assertEqual(books.normalize_exchange_timestamp_ms(now_ms // 1000), now_ms)
        self.assertEqual(books.normalize_exchange_timestamp_ms(now_ms), now_ms)
        self.assertEqual(books.normalize_exchange_timestamp_ms(now_ms * 1000), now_ms)
        parsed = books.parse_causal_book(self._row(now_ms + 1), received_ts_ms=now_ms)
        self.assertIsNotNone(parsed)
        result = books.validate_coherent_books({"t1": parsed}, ["t1"], now_ms=now_ms)
        self.assertFalse(result.ok)
        self.assertEqual(result.reason, "future_exchange_clock")

    def test_hash_age_and_cross_leg_skew_are_binding(self):
        now_ms = int(time.time() * 1000)
        a = books.parse_causal_book(self._row(now_ms - 100, token="a", snapshot_hash="ha"), received_ts_ms=now_ms - 50)
        b = books.parse_causal_book(self._row(now_ms - 200, token="b", snapshot_hash="hb"), received_ts_ms=now_ms - 40)
        self.assertIsNotNone(a)
        self.assertIsNotNone(b)
        assert a is not None and b is not None
        self.assertTrue(books.validate_coherent_books({"a": a, "b": b}, ["a", "b"], now_ms=now_ms).ok)
        stale = replace(a, exchange_ts_ms=now_ms - 6000)
        self.assertEqual(books.validate_coherent_books({"a": stale}, ["a"], now_ms=now_ms).reason, "stale_exchange_book")
        skewed = replace(b, exchange_ts_ms=now_ms - 3000)
        self.assertEqual(books.validate_coherent_books({"a": a, "b": skewed}, ["a", "b"], now_ms=now_ms).reason, "cross_book_exchange_skew")


class LocalFactorCurrentStateTests(unittest.TestCase):
    def test_current_state_requires_every_frozen_control(self):
        times = tuple(range(24))
        c = [0.65 * math.sin(i / 3.0) + 0.04 * ((i % 3) - 1) for i in times]
        d = [0.55 * math.cos(i / 4.0) + 0.03 * ((i % 4) - 1.5) for i in times]
        a = [0.7 * c[i] + 0.2 * d[i] + 0.12 * ((-0.45) ** i) for i in times]
        b = [-0.6 * c[i] + 0.15 * d[i] - 0.10 * ((-0.40) ** i) for i in times]
        panel = lf.standardize_levels({"a": a, "b": b, "c": c, "d": d}, times)
        self.assertIsNotNone(panel)
        assert panel is not None
        fit = lf.fit_pair(panel, "a", "b", min_controls=2)
        self.assertIsInstance(fit, lf.CurrentPairFit)
        assert fit is not None
        at_means = {mid: lf.logistic(panel.means[mid]) for mid in ("a", "b", "c", "d")}
        state = lf.current_residual_state(fit, at_means)
        self.assertIsNotNone(state)
        missing = dict(at_means)
        missing.pop("d")
        self.assertIsNone(lf.current_residual_state(fit, missing))
        shifted = dict(at_means)
        shifted["a"] = lf.logistic(panel.means["a"] + 2.0 * panel.scales["a"])
        shifted_state = lf.current_residual_state(fit, shifted)
        self.assertIsNotNone(shifted_state)
        assert state is not None and shifted_state is not None
        self.assertGreater(abs(shifted_state.residual_z_a), abs(state.residual_z_a) + 0.5)


class PcaCurrentForecastTests(unittest.TestCase):
    def test_single_leg_forecast_contains_common_and_residual_mean(self):
        n = 48
        c = [0.2]
        d = [-0.15]
        r = [0.12]
        for i in range(1, n):
            c.append(0.62 * c[-1] + 0.08 * math.sin(i))
            d.append(0.45 * d[-1] + 0.07 * math.cos(i / 2.0))
            r.append(0.50 * r[-1] + 0.04 * math.sin(i / 3.0))
        target = [0.85 * c[i] - 0.35 * d[i] + r[i] for i in range(n)]
        panel = pca.RawPanel(tuple(range(n)), {"target": tuple(target), "c": tuple(c), "d": tuple(d)})
        model = pca.fit_target(panel, "target", max_components=2, explained_variance_threshold=0.99)
        self.assertIsInstance(model, pca.CurrentPcaTargetModel)
        assert model is not None
        current = {"target": target[-1] + 0.08, "c": c[-1] + 0.20, "d": d[-1] - 0.10}
        score = pca.score_current(model, current, 2)
        self.assertIsNotNone(score)
        assert score is not None
        self.assertTrue(score.common_factor_forecast_identified)
        self.assertAlmostEqual(
            score.predicted_logit_move,
            score.predicted_common_logit_move + score.predicted_residual_logit_move,
            places=12,
        )
        invalid = replace(model, factor_phis=tuple(1.0 for _ in model.factor_phis))
        self.assertIsNone(pca.score_current(invalid, current, 2))


if __name__ == "__main__":
    unittest.main()
