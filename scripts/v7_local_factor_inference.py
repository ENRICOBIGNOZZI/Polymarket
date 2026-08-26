#!/usr/bin/env python3
from __future__ import annotations

import math
import random
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
    """Describe whether Monte Carlo p-value granularity resolves the BH tail.

    Plus-one Monte Carlo p-values cannot be smaller than 1/(B+1).  With m
    hypotheses, an isolated first-ranked discovery at BH level q needs a p-value
    no larger than q/m.  Coarser resolution is still conservative, but zero BH
    discoveries then have weak evidential meaning because a genuinely strong
    isolated hypothesis may be numerically unable to cross the first BH step.
    """
    m = max(0, int(hypotheses))
    b = max(1, int(reps))
    level = min(0.5, max(1e-12, float(q)))
    minimum_attainable = 1.0 / (b + 1.0)
    first_threshold = level / m if m > 0 else 0.0
    required_reps = max(0, math.ceil(m / level) - 1) if m > 0 else 0
    minimum_rank = (
        max(1, math.ceil(minimum_attainable * m / level))
        if m > 0
        else 0
    )
    return {
        "hypotheses": m,
        "repetitions": b,
        "minimum_attainable_pvalue": minimum_attainable,
        "first_rank_bh_threshold": first_threshold,
        "singleton_bh_resolution_adequate": bool(m > 0 and minimum_attainable <= first_threshold),
        "repetitions_required_for_singleton_bh_resolution": required_reps,
        "minimum_rank_needed_if_pvalues_hit_nominal_floor": minimum_rank,
    }


def panel_pair_iut_pvalues(
    panel: core.StandardizedPanel,
    pairs: Sequence[tuple[str, str]] | None = None,
    reps: int = 300,
    seed: int = 20260826,
    min_controls: int = 2,
) -> dict[tuple[str, str], tuple[core.PairFit, float]]:
    """Bootstrap valid marginal unit-root p-values, then form an IUT pair p-value.

    Both targets are excluded from the common factor, so each target's residual
    statistic can be calibrated marginally from the same joint panel-increment
    bootstrap. Calibrating only max(t_A,t_B) under the special case where both
    targets are unit-root is not valid for the full composite null: if one target
    is already stationary and the other is unit-root, that calibration can be
    anti-conservative. The max of the two valid marginal p-values controls the
    intersection-union null before BH is applied across pair hypotheses.
    """
    pair_list = list(pairs or core.all_pairs(panel))
    observed: dict[tuple[str, str], core.PairFit] = {}
    for pair in pair_list:
        fit = core.fit_pair(panel, pair[0], pair[1], min_controls=min_controls)
        if fit is not None:
            observed[pair] = fit
    if not observed:
        return {}

    left_a = {pair: 0 for pair in observed}
    left_b = {pair: 0 for pair in observed}
    valid_reps = {pair: 0 for pair in observed}
    total = max(50, int(reps))
    rng = random.Random(seed)
    for _ in range(total):
        boot = core.null_panel_bootstrap(panel, rng)
        if boot is None:
            continue
        for pair, observed_fit in observed.items():
            fit = core.fit_pair(boot, pair[0], pair[1], min_controls=min_controls)
            if fit is None:
                continue
            valid_reps[pair] += 1
            if fit.adf_a <= observed_fit.adf_a:
                left_a[pair] += 1
            if fit.adf_b <= observed_fit.adf_b:
                left_b[pair] += 1

    output: dict[tuple[str, str], tuple[core.PairFit, float]] = {}
    for pair, fit in observed.items():
        n = valid_reps[pair]
        if n <= 0:
            output[pair] = (fit, 1.0)
            continue
        p_a = (left_a[pair] + 1.0) / (n + 1.0)
        p_b = (left_b[pair] + 1.0) / (n + 1.0)
        output[pair] = (fit, intersection_union_pvalue(p_a, p_b))
    return output
