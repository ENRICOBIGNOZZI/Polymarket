#!/usr/bin/env python3
"""Strict frozen-holdout evaluation for V7 cross-sectional ranking.

The registered 2h/6h challenger is a fixed-model test. Its ridge fit is built
once at the pre-registered holdout boundary using only labels that were mature
before that boundary minus the configured embargo. The same coefficients are
then reused for every holdout prediction. Adaptive/walk-forward evaluation
lives in v7_cross_sectional_rank_inference.py and is intentionally separate.
"""
from __future__ import annotations

import statistics
from typing import Sequence

import v7_cross_sectional_rank_core as core


def history_start_for_frozen_fit(
    *,
    now_ts: int,
    holdout_start_ts: int,
    rolling_lookback_seconds: int,
    training_window_seconds: int,
    bucket_seconds: int,
    max_feature_lag_steps: int = 12,
) -> tuple[int, int]:
    """Return a fetch start that cannot drift through the frozen fit window.

    `raw_features` needs the previous 12 buckets. A rolling 30-day fetch would
    eventually move through the fixed pre-holdout training window, changing a
    supposedly frozen fit across scheduler runs. Keep enough fixed history for
    the complete training window plus feature warm-up, while preserving any
    longer rolling lookback that is already available.
    """
    required_start = (
        int(holdout_start_ts)
        - int(training_window_seconds)
        - int(max_feature_lag_steps) * int(bucket_seconds)
    )
    rolling_start = int(now_ts) - int(rolling_lookback_seconds)
    return min(rolling_start, required_start), required_start


def fit_at_holdout_boundary(
    rows: Sequence[core.TrainingRow],
    *,
    holdout_start_ts: int,
    window_seconds: int,
    embargo_seconds: int,
    ridge: float,
    half_life_seconds: int,
    min_train_rows: int,
    min_train_cross_sections: int,
) -> core.RidgeFit | None:
    """Fit once using only labels available before the frozen boundary."""
    return core.fit_ridge(
        rows,
        asof_ts=int(holdout_start_ts),
        window_seconds=int(window_seconds),
        embargo_seconds=int(embargo_seconds),
        ridge=float(ridge),
        half_life_seconds=int(half_life_seconds),
        min_rows=int(min_train_rows),
        min_cross_sections=int(min_train_cross_sections),
    )


def frozen_section_metrics(
    rows: Sequence[core.TrainingRow],
    fit: core.RidgeFit | None,
    *,
    holdout_start_ts: int,
    tail_fraction: float,
) -> list[dict[str, float | int]]:
    """Score every matured holdout section with one immutable fit."""
    if fit is None:
        return []
    by_ts: dict[int, list[core.TrainingRow]] = {}
    for row in rows:
        if int(row.ts) >= int(holdout_start_ts):
            by_ts.setdefault(int(row.ts), []).append(row)

    out: list[dict[str, float | int]] = []
    for eval_ts in sorted(by_ts):
        section = by_ts[eval_ts]
        if len(section) < 5:
            continue
        pred = [sum(b * x for b, x in zip(fit.beta, row.features)) for row in section]
        true = [row.target_logit for row in section]
        rank_ic = core.spearman(pred, true)
        order = sorted(range(len(section)), key=lambda index: pred[index])
        n_tail = max(1, int(len(order) * float(tail_fraction)))
        bottom = order[:n_tail]
        top = order[-n_tail:]
        spread = (
            statistics.fmean(true[index] for index in top)
            - statistics.fmean(true[index] for index in bottom)
        )
        valid_hits = [
            int((forecast > 0.0) == (realized > 0.0))
            for forecast, realized in zip(pred, true)
            if abs(forecast) > 1e-12 and abs(realized) > 1e-12
        ]
        out.append(
            {
                "ts": int(eval_ts),
                "rank_ic": float(rank_ic),
                "top_bottom_logit_spread": float(spread),
                "directional_hit_rate": (
                    float(sum(valid_hits) / len(valid_hits)) if valid_hits else 0.0
                ),
                "n": len(section),
            }
        )
    return out


def evaluate(
    rows: Sequence[core.TrainingRow],
    *,
    holdout_start_ts: int,
    window_seconds: int,
    embargo_seconds: int,
    ridge: float,
    half_life_seconds: int,
    min_train_rows: int,
    min_train_cross_sections: int,
    tail_fraction: float,
) -> tuple[core.RidgeFit | None, list[dict[str, float | int]]]:
    fit = fit_at_holdout_boundary(
        rows,
        holdout_start_ts=holdout_start_ts,
        window_seconds=window_seconds,
        embargo_seconds=embargo_seconds,
        ridge=ridge,
        half_life_seconds=half_life_seconds,
        min_train_rows=min_train_rows,
        min_train_cross_sections=min_train_cross_sections,
    )
    return fit, frozen_section_metrics(
        rows,
        fit,
        holdout_start_ts=holdout_start_ts,
        tail_fraction=tail_fraction,
    )
