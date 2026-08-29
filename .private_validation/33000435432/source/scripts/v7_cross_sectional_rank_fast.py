#!/usr/bin/env python3
from __future__ import annotations

import math
import statistics
from dataclasses import dataclass
from typing import Sequence

import v7_cross_sectional_rank_core as core


@dataclass
class _SectionMoments:
    ts: int
    n_rows: int
    weight_sum: float
    xtx: list[list[float]]
    xty: list[float]


def _zero_matrix(p: int) -> list[list[float]]:
    return [[0.0] * p for _ in range(p)]


def _section_moments(
    rows: Sequence[core.TrainingRow],
    *,
    origin_label_ts: int,
    half_life_seconds: int,
) -> _SectionMoments:
    if not rows:
        raise ValueError("section rows required")
    p = len(core.FEATURE_NAMES)
    xtx = _zero_matrix(p)
    xty = [0.0] * p
    weight_sum = 0.0
    for row in rows:
        # This base weight is proportional to the canonical as-of weight for every
        # evaluation timestamp. The omitted proportionality factor is common to all
        # eligible rows and therefore cancels from both ridge normal equations and
        # ridge * sum(weights) regularization.
        weight = 0.5 ** (
            (float(origin_label_ts) - float(row.label_ts))
            / max(1.0, float(half_life_seconds))
        )
        weight_sum += weight
        for j in range(p):
            xj = row.features[j]
            xty[j] += weight * xj * row.target_logit
            for k in range(j, p):
                xtx[j][k] += weight * xj * row.features[k]
    for j in range(p):
        for k in range(j):
            xtx[j][k] = xtx[k][j]
    return _SectionMoments(rows[0].ts, len(rows), weight_sum, xtx, xty)


def _add_moments(
    dst_xtx: list[list[float]],
    dst_xty: list[float],
    moments: _SectionMoments,
    sign: float,
) -> None:
    p = len(dst_xty)
    for j in range(p):
        dst_xty[j] += sign * moments.xty[j]
        for k in range(p):
            dst_xtx[j][k] += sign * moments.xtx[j][k]


def _beta_from_window(
    xtx: list[list[float]],
    xty: list[float],
    weight_sum: float,
    ridge: float,
) -> tuple[float, ...]:
    p = len(xty)
    system = [list(row) for row in xtx]
    penalty = max(1e-8, float(ridge)) * max(1.0, float(weight_sum))
    for j in range(p):
        system[j][j] += penalty
    return tuple(core._solve_linear(system, list(xty)))


def walk_forward_evaluate(
    rows: Sequence[core.TrainingRow],
    bucket_seconds: int,
    horizon_steps: int,
    window_seconds: int,
    embargo_steps: int = 1,
    ridge: float = 0.05,
    half_life_seconds: int = 7 * 86400,
    min_train_rows: int = 100,
    min_train_cross_sections: int = 20,
    tail_fraction: float = 0.2,
) -> dict[str, object]:
    """Exact-beta rolling equivalent of ``core.walk_forward_evaluate``.

    The original evaluator re-scans all rows for every cross-section. At a fixed
    horizon, every row in section ``s`` has ``label_ts = s + h``. Eligibility is
    therefore the monotone interval

      asof-window <= s <= asof-embargo-horizon.

    Recency weights at any as-of differ from fixed section base weights by one
    multiplicative constant common to the entire eligible window. Because the ridge
    penalty is ``ridge * sum(weights)``, that constant cancels from the normal
    equations. We can thus maintain weighted X'X/X'y exactly with a sliding window.

    Only beta is needed by historical ranking metrics; the expensive residual MAD
    calculated by ``fit_ridge`` is intentionally left to the final current-time fit
    where sigma is required for executable uncertainty.
    """
    by_ts: dict[int, list[core.TrainingRow]] = {}
    for row in rows:
        by_ts.setdefault(row.ts, []).append(row)
    section_times = sorted(by_ts)
    if not section_times:
        return {
            "horizon_steps": horizon_steps,
            "cross_sections": 0,
            "predictions": 0,
            "mean_rank_ic": 0.0,
            "median_rank_ic": 0.0,
            "positive_ic_fraction": 0.0,
            "mean_top_bottom_logit_spread": 0.0,
            "median_top_bottom_logit_spread": 0.0,
            "directional_hit_rate": 0.0,
            "mean_turnover": 0.0,
            "decile_target_means": [0.0] * 10,
            "decile_monotonicity": 0.0,
            "economic_pnl_validated": False,
        }

    origin_label_ts = max(row.label_ts for row in rows)
    moments = [
        _section_moments(
            by_ts[ts],
            origin_label_ts=origin_label_ts,
            half_life_seconds=half_life_seconds,
        )
        for ts in section_times
    ]
    p = len(core.FEATURE_NAMES)
    window_xtx = _zero_matrix(p)
    window_xty = [0.0] * p
    window_weight_sum = 0.0
    window_rows = 0
    left = 0
    right = 0

    ics: list[float] = []
    spreads: list[float] = []
    hits: list[int] = []
    selected_sets: list[set[str]] = []
    deciles: dict[int, list[float]] = {i: [] for i in range(10)}
    predictions = 0
    fits = 0
    horizon_seconds = int(horizon_steps) * int(bucket_seconds)
    embargo_seconds = int(embargo_steps) * int(bucket_seconds)

    for eval_ts in section_times:
        lower = eval_ts - int(window_seconds)
        upper = eval_ts - embargo_seconds - horizon_seconds

        while right < len(section_times) and section_times[right] <= upper:
            item = moments[right]
            _add_moments(window_xtx, window_xty, item, +1.0)
            window_weight_sum += item.weight_sum
            window_rows += item.n_rows
            right += 1
        while left < right and section_times[left] < lower:
            item = moments[left]
            _add_moments(window_xtx, window_xty, item, -1.0)
            window_weight_sum -= item.weight_sum
            window_rows -= item.n_rows
            left += 1

        n_sections = right - left
        if window_rows < int(min_train_rows) or n_sections < int(min_train_cross_sections):
            continue
        beta = _beta_from_window(window_xtx, window_xty, window_weight_sum, ridge)
        section = by_ts[eval_ts]
        if len(section) < 5:
            continue
        pred = [sum(b * x for b, x in zip(beta, row.features)) for row in section]
        true = [row.target_logit for row in section]
        fits += 1
        predictions += len(section)
        ics.append(core.spearman(pred, true))
        hits.extend(
            int((forecast > 0) == (realized > 0))
            for forecast, realized in zip(pred, true)
            if abs(forecast) > 1e-12 and abs(realized) > 1e-12
        )
        order = sorted(range(len(section)), key=lambda i: pred[i])
        n_tail = max(1, int(len(order) * tail_fraction))
        bottom, top = order[:n_tail], order[-n_tail:]
        spreads.append(
            statistics.fmean(true[i] for i in top)
            - statistics.fmean(true[i] for i in bottom)
        )
        selected_sets.append({section[i].market_id for i in top + bottom})
        for pos, index in enumerate(order):
            decile = min(9, int(10 * pos / max(1, len(order))))
            deciles[decile].append(true[index])

    turnover: list[float] = []
    for first, second in zip(selected_sets, selected_sets[1:]):
        union = first | second
        turnover.append(1.0 - len(first & second) / max(1, len(union)))
    decile_means = [
        statistics.fmean(deciles[index]) if deciles[index] else 0.0
        for index in range(10)
    ]
    return {
        "horizon_steps": horizon_steps,
        "cross_sections": fits,
        "predictions": predictions,
        "mean_rank_ic": statistics.fmean(ics) if ics else 0.0,
        "median_rank_ic": core.median(ics),
        "positive_ic_fraction": sum(value > 0 for value in ics) / len(ics) if ics else 0.0,
        "mean_top_bottom_logit_spread": statistics.fmean(spreads) if spreads else 0.0,
        "median_top_bottom_logit_spread": core.median(spreads),
        "directional_hit_rate": sum(hits) / len(hits) if hits else 0.0,
        "mean_turnover": statistics.fmean(turnover) if turnover else 0.0,
        "decile_target_means": decile_means,
        "decile_monotonicity": core.pearson(list(range(10)), decile_means),
        "economic_pnl_validated": False,
    }
