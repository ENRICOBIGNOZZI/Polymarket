#!/usr/bin/env python3
from __future__ import annotations

import math
import random
import statistics
from collections.abc import Sequence

import v7_local_factor_core as core


def intersection_union_pvalue(p_a: float, p_b: float) -> float:
    """Valid pair p-value when the alternative requires both residuals stationary.

    The composite null is H0 = {A has a unit root} OR {B has a unit root}.
    For an intersection-union test the pair can reject only when both marginal
    nulls reject, hence p_pair = max(p_A, p_B).
    """
    if not (math.isfinite(p_a) and math.isfinite(p_b)):
        return 1.0
    return min(1.0, max(0.0, max(p_a, p_b)))


def bh_resolution_diagnostics(hypotheses: int, reps: int, q: float) -> dict[str, float | int | bool]:
    """Describe whether Monte Carlo p-value granularity resolves the BH tail."""
    m = max(0, int(hypotheses))
    b = max(1, int(reps))
    level = min(0.5, max(1e-12, float(q)))
    minimum_attainable = 1.0 / (b + 1.0)
    first_threshold = level / m if m > 0 else 0.0
    required_reps = max(0, math.ceil(m / level) - 1) if m > 0 else 0
    minimum_rank = max(1, math.ceil(minimum_attainable * m / level)) if m > 0 else 0
    return {
        "hypotheses": m,
        "repetitions": b,
        "minimum_attainable_pvalue": minimum_attainable,
        "first_rank_bh_threshold": first_threshold,
        "singleton_bh_resolution_adequate": bool(m > 0 and minimum_attainable <= first_threshold),
        "repetitions_required_for_singleton_bh_resolution": required_reps,
        "minimum_rank_needed_if_pvalues_hit_nominal_floor": minimum_rank,
    }


def _pair_factor(panel: core.StandardizedPanel, pair: tuple[str, str], min_controls: int) -> tuple[float, ...] | None:
    result = core.pair_control_factor(panel, pair[0], pair[1], min_controls=min_controls)
    return None if result is None else result[1]


def _standardize(values: Sequence[float]) -> tuple[float, ...] | None:
    vals = tuple(float(x) for x in values)
    if len(vals) < 4 or not all(math.isfinite(x) for x in vals):
        return None
    mu = statistics.fmean(vals)
    sd = statistics.stdev(vals)
    if sd <= 1e-10:
        return None
    return tuple((x - mu) / sd for x in vals)


def _residual_null_components(
    factor: Sequence[float],
    loading: float,
    residual: Sequence[float],
) -> tuple[float, tuple[float, ...]] | None:
    if len(factor) != len(residual) or len(residual) < 12 or not math.isfinite(loading):
        return None
    diffs = tuple(float(residual[i] - residual[i - 1]) for i in range(1, len(residual)))
    drift = statistics.fmean(diffs)
    centered = tuple(x - drift for x in diffs)
    if not any(abs(x) > 1e-12 for x in centered):
        return None
    return drift, centered


def _bootstrapped_target_adf(
    factor: Sequence[float],
    observed_loading: float,
    residual_start: float,
    drift: float,
    centered_increments: Sequence[float],
    indices: Sequence[int],
) -> float | None:
    """Generate one marginal unit-root null path conditional on pair controls."""
    if len(centered_increments) + 1 != len(factor):
        return None
    path = [float(residual_start)]
    for idx in indices:
        path.append(path[-1] + drift + centered_increments[idx])
    target = _standardize(tuple(observed_loading * f + u for f, u in zip(factor, path)))
    if target is None:
        return None
    loading = core.ols_loading(target, factor)
    if loading is None:
        return None
    residual = tuple(y - loading * f for y, f in zip(target, factor))
    return core.adf_t_stat(residual)


def marginal_residual_unit_root_pvalue(
    panel: core.StandardizedPanel,
    pair: tuple[str, str],
    target: str,
    reps: int = 300,
    seed: int = 20260826,
    min_controls: int = 2,
) -> tuple[core.PairFit, float] | None:
    """Conditional marginal unit-root bootstrap for one target in a pair.

    The target-free control PC is held fixed exactly as observed. Only the tested
    target residual is imposed to be I(1); its dependent increments are block
    resampled and the residual level path is reconstructed before target loading,
    residual and ADF are refit. This preserves the composite-null nuisance path
    while using the same orientation-invariant factor definition as the observed
    pair fit.
    """
    fit = core.fit_pair(panel, pair[0], pair[1], min_controls=min_controls)
    if fit is None or target not in pair:
        return None
    factor = _pair_factor(panel, pair, min_controls)
    if factor is None:
        return None
    if target == pair[0]:
        loading, residual, observed_stat = fit.loading_a, fit.residual_a, fit.adf_a
    else:
        loading, residual, observed_stat = fit.loading_b, fit.residual_b, fit.adf_b
    components = _residual_null_components(factor, loading, residual)
    if components is None:
        return fit, 1.0
    drift, centered = components
    ninc = len(centered)
    block = max(2, min(ninc, int(round(math.sqrt(ninc)))))
    total = max(50, int(reps))
    rng = random.Random(seed)
    left, valid = 0, 0
    for _ in range(total):
        indices = core._joint_block_indices(ninc, block, rng)
        stat = _bootstrapped_target_adf(
            factor,
            loading,
            residual[0],
            drift,
            centered,
            indices,
        )
        if stat is None:
            continue
        valid += 1
        if stat <= observed_stat:
            left += 1
    if valid <= 0:
        return fit, 1.0
    return fit, (left + 1.0) / (valid + 1.0)


def panel_pair_iut_pvalues(
    panel: core.StandardizedPanel,
    pairs: Sequence[tuple[str, str]] | None = None,
    reps: int = 300,
    seed: int = 20260826,
    min_controls: int = 2,
) -> dict[tuple[str, str], tuple[core.PairFit, float]]:
    """Calibrate marginal residual-unit-root nulls, then form IUT pair p-values."""
    pair_list = sorted(set(pairs or core.all_pairs(panel)))
    output: dict[tuple[str, str], tuple[core.PairFit, float]] = {}
    for i, pair in enumerate(pair_list):
        fit = core.fit_pair(panel, pair[0], pair[1], min_controls=min_controls)
        if fit is None:
            continue
        a_result = marginal_residual_unit_root_pvalue(
            panel,
            pair,
            pair[0],
            reps=reps,
            seed=seed + 2 * i,
            min_controls=min_controls,
        )
        b_result = marginal_residual_unit_root_pvalue(
            panel,
            pair,
            pair[1],
            reps=reps,
            seed=seed + 2 * i + 1,
            min_controls=min_controls,
        )
        if a_result is None or b_result is None:
            output[pair] = (fit, 1.0)
            continue
        p_a = a_result[1]
        p_b = b_result[1]
        output[pair] = (fit, intersection_union_pvalue(p_a, p_b))
    return output
