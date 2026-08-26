#!/usr/bin/env python3
"""Frozen-holdout evaluation for V7 cross-sectional ranking.

This module is deliberately separate from the adaptive/walk-forward inference
path.  A frozen holdout fits once at the pre-registered holdout boundary using
only labels that were mature before the boundary minus the embargo, then reuses
that exact fit for every holdout cross-section.
"""
from __future__ import annotations

import statistics
from typing import Sequence

import v7_cross_sectional_rank_core as core


def frozen_training_label_cutoff_ts(
    holdout_start_ts: int,
    *,
    bucket_seconds: int,
    embargo_steps: int,
) -> int:
    return int(holdout_start_ts) - int(bucket_seconds) * int(embargo_steps)


def fit_frozen_model(
    rows: Sequence[core.TrainingRow],
    *,
    holdout_start_ts: int,
    bucket_seconds: int,
    window_seconds: int,
    embargo_steps: int,
    ridge: float,
    half_life_seconds: int,
    min_train_rows: int,
    min_train_cross_sections: int,
) -> core.RidgeFit | None:
    """Fit exactly at the holdout boundary, never at an evaluation timestamp."""
    return core.fit_ridge(
        rows,
        asof_ts=int(holdout_start_ts),
        window_seconds=int(window_seconds),
        embargo_seconds=int(embargo_steps) * int(bucket_seconds),
        ridge=float(ridge),
        half_life_seconds=int(half_life_seconds),
        min_rows=int(min_train_rows),
        min_cross_sections=int(min_train_cross_sections),
    )


def frozen_section_metrics(
    rows: Sequence[core.TrainingRow],
    *,
    holdout_start_ts: int,
    bucket_seconds: int,
    window_seconds: int,
    embargo_steps: int,
    ridge: float,
    half_life_seconds: int,
    min_train_rows: int,
    min_train_cross_sections: int,
    tail_fraction: float,
) -> tuple[list[dict[str, float | int]], core.RidgeFit | None]:
    """Evaluate holdout sections with one pre-holdout fit.

    Post-holdout labels may be used only as evaluation targets.  They can never
    enter the fitted coefficients because ``fit_frozen_model`` fixes ``asof_ts``
    at ``holdout_start_ts``.
    """
    fit = fit_frozen_model(
        rows,
        holdout_start_ts=holdout_start_ts,
        bucket_seconds=bucket_seconds,
        window_seconds=window_seconds,
        embargo_steps=embargo_steps,
        ridge=ridge,
        half_life_seconds=half_life_seconds,
        min_train_rows=min_train_rows,
        min_train_cross_sections=min_train_cross_sections,
    )
    if fit is None:
        return [], None

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
    return out, fit
