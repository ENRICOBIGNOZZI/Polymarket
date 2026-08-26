#!/usr/bin/env python3
from __future__ import annotations

import importlib.util
import math
import random
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location(
    "v7_cross_sectional_rank_core",
    ROOT / "scripts" / "v7_cross_sectional_rank_core.py",
)
assert SPEC is not None and SPEC.loader is not None
xr = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(xr)


class V7CrossSectionalRankTests(unittest.TestCase):
    def test_common_forward_shock_is_removed_from_relative_target(self) -> None:
        meta = [
            xr.MarketMeta("a", "ea", "g"),
            xr.MarketMeta("b", "eb", "g"),
            xr.MarketMeta("c", "ec", "g"),
        ]
        target = xr.target_residuals(
            [(meta[0], 0.2), (meta[1], 0.2), (meta[2], 0.2)],
            group_weight=0.5,
            min_group_size=2,
        )
        self.assertTrue(all(abs(value) < 1e-12 for value in target.values()))

    def test_fit_is_purged_and_cannot_use_label_arriving_at_asof(self) -> None:
        rows = [
            xr.TrainingRow(
                ts=100,
                label_ts=160,
                market_id=f"m{i}",
                event_id=f"e{i}",
                group="g",
                probability=0.5,
                features=(1.0,) * len(xr.FEATURE_NAMES),
                target_logit=0.1,
            )
            for i in range(200)
        ]
        self.assertIsNone(
            xr.fit_ridge(
                rows,
                asof_ts=160,
                window_seconds=1000,
                embargo_seconds=60,
                min_rows=100,
                min_cross_sections=1,
            )
        )

    def test_ridge_recovers_cross_sectional_predictor(self) -> None:
        random.seed(11)
        rows = []
        p = len(xr.FEATURE_NAMES)
        for section in range(30):
            ts = 1000 + section * 60
            for market in range(20):
                signal = (market - 9.5) / 5.0
                features = [0.0] * p
                features[0] = signal
                features[1] = 0.25 * signal + 0.1 * random.gauss(0.0, 1.0)
                target = 0.08 * signal + 0.01 * random.gauss(0.0, 1.0)
                rows.append(
                    xr.TrainingRow(
                        ts=ts,
                        label_ts=ts + 30,
                        market_id=f"m{market}",
                        event_id=f"e{market}",
                        group="all",
                        probability=0.5,
                        features=tuple(features),
                        target_logit=target,
                    )
                )
        fit = xr.fit_ridge(
            rows,
            asof_ts=4000,
            window_seconds=10000,
            embargo_seconds=1,
            ridge=0.01,
            half_life_seconds=100000,
            min_rows=100,
            min_cross_sections=10,
        )
        self.assertIsNotNone(fit)
        assert fit is not None
        self.assertGreater(fit.beta[0], 0.02)
        self.assertGreater(fit.n_cross_sections, 20)

    def test_positive_prediction_maps_to_yes_and_negative_to_no(self) -> None:
        yes_score = xr.ScoreRow(
            100, "m1", "e1", "g", 0.5,
            (0.0,) * len(xr.FEATURE_NAMES), 0.4, 0.1,
        )
        no_score = xr.ScoreRow(
            100, "m2", "e2", "g", 0.5,
            (0.0,) * len(xr.FEATURE_NAMES), -0.4, 0.1,
        )
        yes_book = xr.BookEconomics(
            "m1", "e1", 0.49, 0.51, 0.49, 0.51,
            100.0, 0.04, 1.0, True, True, 100,
        )
        no_book = xr.BookEconomics(
            "m2", "e2", 0.49, 0.51, 0.49, 0.51,
            100.0, 0.04, 1.0, True, True, 100,
        )
        yes = xr.candidate_from_score(yes_score, yes_book, 3600, 100, 0, 0, 0, 30)
        no = xr.candidate_from_score(no_score, no_book, 3600, 100, 0, 0, 0, 30)
        self.assertEqual(yes.side, "YES")
        self.assertEqual(no.side, "NO")

    def test_unknown_fee_schedule_fails_closed(self) -> None:
        score = xr.ScoreRow(
            100, "m", "e", "g", 0.5,
            (0.0,) * len(xr.FEATURE_NAMES), 0.4, 0.1,
        )
        book = xr.BookEconomics(
            "m", "e", 0.49, 0.51, 0.49, 0.51,
            100.0, 0.04, 1.0, True, False, 100,
        )
        self.assertIsNone(xr.candidate_from_score(score, book, 3600, 100, 0, 0, 0, 30))

    def test_stale_book_fails_closed(self) -> None:
        score = xr.ScoreRow(
            100, "m", "e", "g", 0.5,
            (0.0,) * len(xr.FEATURE_NAMES), 0.4, 0.1,
        )
        book = xr.BookEconomics(
            "m", "e", 0.49, 0.51, 0.49, 0.51,
            100.0, 0.0, 1.0, True, True, 1,
        )
        self.assertIsNone(xr.candidate_from_score(score, book, 3600, 100, 0, 0, 0, 30))

    def test_capital_time_penalizes_same_edge_at_longer_horizon(self) -> None:
        score = xr.ScoreRow(
            100, "m", "e", "g", 0.5,
            (0.0,) * len(xr.FEATURE_NAMES), 0.5, 0.1,
        )
        book = xr.BookEconomics(
            "m", "e", 0.49, 0.51, 0.49, 0.51,
            100.0, 0.0, 1.0, True, True, 100,
        )
        short = xr.candidate_from_score(score, book, 1800, 100, 0, 1, 0, 30)
        long = xr.candidate_from_score(score, book, 21600, 100, 0, 1, 0, 30)
        assert short is not None and long is not None
        self.assertGreater(short.economic_score, long.economic_score)

    def test_selection_allows_only_one_contract_per_event(self) -> None:
        scores = [
            xr.ScoreRow(100, "a", "same", "g", 0.5, (0.0,) * len(xr.FEATURE_NAMES), 0.6, 0.1),
            xr.ScoreRow(100, "b", "same", "g", 0.5, (0.0,) * len(xr.FEATURE_NAMES), 0.5, 0.1),
        ]
        books = {
            market: xr.BookEconomics(
                market, "same", 0.49, 0.50, 0.49, 0.50,
                100.0, 0.0, 1.0, True, True, 100,
            )
            for market in ("a", "b")
        }
        selected = xr.select_candidates(
            scores,
            books,
            horizon_seconds=3600,
            now=100,
            min_net_edge=0.0001,
            max_positions_per_side=5,
            max_trade_usd=60.0,
            sleeve_budget_usd=500.0,
            slippage_bps=0.0,
            capital_cost_bps_per_hour=0.0,
            adverse_penalty_bps=0.0,
        )
        self.assertEqual(len(selected), 1)

    def test_historical_evaluator_never_claims_economic_pnl(self) -> None:
        report = xr.walk_forward_evaluate(
            [],
            bucket_seconds=1800,
            horizon_steps=1,
            window_seconds=7 * 86400,
        )
        self.assertFalse(report["economic_pnl_validated"])


if __name__ == "__main__":
    unittest.main()
