#!/usr/bin/env python3
from __future__ import annotations

import math
import random
import statistics
from collections import defaultdict
from typing import Sequence

import v7_cross_sectional_rank_core as core
import v7_cross_sectional_rank_fast as fast


def _rolling_section_metrics(
    rows: Sequence[core.TrainingRow],
    *,
    bucket_seconds: int,
    horizon_steps: int,
    window_seconds: int,
    embargo_steps: int,
    ridge: float,
    half_life_seconds: int,
    min_train_rows: int,
    min_train_cross_sections: int,
    tail_fraction: float,
) -> list[dict[str, float | int]]:
    by_ts: dict[int, list[core.TrainingRow]] = {}
    for row in rows:
        by_ts.setdefault(row.ts, []).append(row)
    section_times = sorted(by_ts)
    if not section_times:
        return []

    origin_label_ts = max(row.label_ts for row in rows)
    moments = [
        fast._section_moments(
            by_ts[ts],
            origin_label_ts=origin_label_ts,
            half_life_seconds=half_life_seconds,
        )
        for ts in section_times
    ]
    p = len(core.FEATURE_NAMES)
    xtx = [[0.0] * p for _ in range(p)]
    xty = [0.0] * p
    weight_sum = 0.0
    window_rows = 0
    left = 0
    right = 0
    horizon_seconds = int(horizon_steps) * int(bucket_seconds)
    embargo_seconds = int(embargo_steps) * int(bucket_seconds)
    out: list[dict[str, float | int]] = []

    for eval_ts in section_times:
        lower = eval_ts - int(window_seconds)
        upper = eval_ts - embargo_seconds - horizon_seconds
        while right < len(section_times) and section_times[right] <= upper:
            item = moments[right]
            fast._add_moments(xtx, xty, item, +1.0)
            weight_sum += item.weight_sum
            window_rows += item.n_rows
            right += 1
        while left < right and section_times[left] < lower:
            item = moments[left]
            fast._add_moments(xtx, xty, item, -1.0)
            weight_sum -= item.weight_sum
            window_rows -= item.n_rows
            left += 1
        if window_rows < int(min_train_rows) or right - left < int(min_train_cross_sections):
            continue
        section = by_ts[eval_ts]
        if len(section) < 5:
            continue
        beta = fast._beta_from_window(xtx, xty, weight_sum, ridge)
        pred = [sum(b * x for b, x in zip(beta, row.features)) for row in section]
        true = [row.target_logit for row in section]
        rank_ic = core.spearman(pred, true)
        order = sorted(range(len(section)), key=lambda index: pred[index])
        n_tail = max(1, int(len(order) * tail_fraction))
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


def _daily_means(metrics: list[dict[str, float | int]], key: str) -> list[tuple[int, float]]:
    grouped: dict[int, list[float]] = defaultdict(list)
    for row in metrics:
        value = float(row[key])
        if math.isfinite(value):
            grouped[int(row["ts"]) // 86400].append(value)
    return [
        (day, statistics.fmean(values))
        for day, values in sorted(grouped.items())
        if values
    ]


def _bootstrap_nonpositive_probability(
    daily: list[tuple[int, float]],
    *,
    samples: int,
    seed: int,
) -> float | None:
    values = [value for _day, value in daily if math.isfinite(value)]
    if len(values) < 2 or samples <= 0:
        return None
    rng = random.Random(seed)
    nonpositive = 0
    for _ in range(samples):
        draw = [rng.choice(values) for _ in values]
        nonpositive += int(statistics.fmean(draw) <= 0.0)
    return nonpositive / samples


def _half_means(daily: list[tuple[int, float]]) -> tuple[float | None, float | None]:
    if len(daily) < 2:
        return None, None
    midpoint = max(1, len(daily) // 2)
    first = [value for _day, value in daily[:midpoint]]
    second = [value for _day, value in daily[midpoint:]]
    return (
        statistics.fmean(first) if first else None,
        statistics.fmean(second) if second else None,
    )


def blocked_inference(
    metrics: list[dict[str, float | int]],
    *,
    bootstrap_samples: int = 4999,
    seed: int = 20260826,
) -> dict[str, object]:
    ic_daily = _daily_means(metrics, "rank_ic")
    spread_daily = _daily_means(metrics, "top_bottom_logit_spread")
    ic_values = [value for _day, value in ic_daily]
    spread_values = [value for _day, value in spread_daily]
    ic_half = _half_means(ic_daily)
    spread_half = _half_means(spread_daily)
    return {
        "block": "UTC_day",
        "cross_sections": len(metrics),
        "days": len(ic_daily),
        "bootstrap_samples": int(bootstrap_samples),
        "mean_daily_rank_ic": statistics.fmean(ic_values) if ic_values else 0.0,
        "median_daily_rank_ic": statistics.median(ic_values) if ic_values else 0.0,
        "positive_daily_rank_ic_fraction": (
            sum(value > 0.0 for value in ic_values) / len(ic_values) if ic_values else 0.0
        ),
        "rank_ic_bootstrap_p_mean_nonpositive": _bootstrap_nonpositive_probability(
            ic_daily,
            samples=bootstrap_samples,
            seed=seed,
        ),
        "rank_ic_first_half_mean": ic_half[0],
        "rank_ic_second_half_mean": ic_half[1],
        "mean_daily_top_bottom_logit_spread": (
            statistics.fmean(spread_values) if spread_values else 0.0
        ),
        "median_daily_top_bottom_logit_spread": (
            statistics.median(spread_values) if spread_values else 0.0
        ),
        "positive_daily_top_bottom_fraction": (
            sum(value > 0.0 for value in spread_values) / len(spread_values)
            if spread_values
            else 0.0
        ),
        "top_bottom_bootstrap_p_mean_nonpositive": _bootstrap_nonpositive_probability(
            spread_daily,
            samples=bootstrap_samples,
            seed=seed + 1,
        ),
        "top_bottom_first_half_mean": spread_half[0],
        "top_bottom_second_half_mean": spread_half[1],
    }


def discovery_robustness_gate(inference: dict[str, object]) -> tuple[bool, list[str]]:
    """Pre-specified robustness screen; never a promotion gate.

    This gate is intentionally stricter than merely seeing a positive aggregate IC.
    It requires at least 20 UTC-day blocks, positive average IC and tail spread in
    both temporal halves, >55% positive daily blocks for both metrics, and one-sided
    day-block bootstrap p <= 5% for the mean of each metric.
    """
    reasons: list[str] = []
    if int(inference.get("days") or 0) < 20:
        reasons.append("insufficient_day_blocks")
    if float(inference.get("positive_daily_rank_ic_fraction") or 0.0) < 0.55:
        reasons.append("daily_rank_ic_stability")
    if float(inference.get("positive_daily_top_bottom_fraction") or 0.0) < 0.55:
        reasons.append("daily_tail_spread_stability")
    p_ic = inference.get("rank_ic_bootstrap_p_mean_nonpositive")
    if p_ic is None or float(p_ic) > 0.05:
        reasons.append("rank_ic_block_bootstrap")
    p_spread = inference.get("top_bottom_bootstrap_p_mean_nonpositive")
    if p_spread is None or float(p_spread) > 0.05:
        reasons.append("tail_spread_block_bootstrap")
    for key in (
        "rank_ic_first_half_mean",
        "rank_ic_second_half_mean",
        "top_bottom_first_half_mean",
        "top_bottom_second_half_mean",
    ):
        value = inference.get(key)
        if value is None or float(value) <= 0.0:
            reasons.append(key)
    return not reasons, reasons


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
    aggregate = fast.walk_forward_evaluate(
        rows,
        bucket_seconds=bucket_seconds,
        horizon_steps=horizon_steps,
        window_seconds=window_seconds,
        embargo_steps=embargo_steps,
        ridge=ridge,
        half_life_seconds=half_life_seconds,
        min_train_rows=min_train_rows,
        min_train_cross_sections=min_train_cross_sections,
        tail_fraction=tail_fraction,
    )
    metrics = _rolling_section_metrics(
        rows,
        bucket_seconds=bucket_seconds,
        horizon_steps=horizon_steps,
        window_seconds=window_seconds,
        embargo_steps=embargo_steps,
        ridge=ridge,
        half_life_seconds=half_life_seconds,
        min_train_rows=min_train_rows,
        min_train_cross_sections=min_train_cross_sections,
        tail_fraction=tail_fraction,
    )
    inference = blocked_inference(metrics)
    robust, reasons = discovery_robustness_gate(inference)
    aggregate["blocked_inference"] = inference
    aggregate["discovery_robustness_gate"] = robust
    aggregate["discovery_robustness_gate_reasons"] = reasons
    return aggregate
