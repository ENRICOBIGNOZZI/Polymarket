#!/usr/bin/env python3
from __future__ import annotations

import math
import random
import re
import statistics
from dataclasses import dataclass, replace
from typing import Mapping, Sequence

import v7_pca_stat_arb_core as core

_TOKEN = re.compile(r"[a-z0-9]+")


@dataclass(frozen=True)
class TargetControlPlan:
    target: str
    controls: tuple[str, ...]
    method: str = "metadata_only_same_event_then_text_similarity"


def _question_tokens(question: str) -> set[str]:
    return {
        token
        for token in _TOKEN.findall(str(question).lower())
        if len(token) > 1 and not token.isdigit()
    }


def _jaccard(a: set[str], b: set[str]) -> float:
    union = a | b
    return len(a & b) / len(union) if union else 0.0


def predeclare_target_controls(
    markets: Sequence[object],
    target_id: str,
    *,
    minimum_controls: int = 2,
    maximum_controls: int = 8,
) -> TargetControlPlan | None:
    """Freeze a bounded nuisance-control plan using metadata only.

    The selection occurs before price history is fetched.  Liquidity, missingness,
    prices, returns, residuals, p-values, z-scores, edge and PnL are deliberately
    absent from the ranking.  Missing predeclared controls later make the target
    hypothesis unestimable; there is no ex-post replacement.
    """
    by_id = {str(getattr(market, "market_id", "")): market for market in markets}
    target = by_id.get(str(target_id))
    if target is None:
        return None
    target_event = str(getattr(target, "event_id", ""))
    target_tokens = _question_tokens(str(getattr(target, "question", "")))
    ranked: list[tuple[int, float, str]] = []
    for market_id, market in by_id.items():
        if market_id == target_id:
            continue
        same_event = int(bool(target_event) and str(getattr(market, "event_id", "")) == target_event)
        similarity = _jaccard(target_tokens, _question_tokens(str(getattr(market, "question", ""))))
        ranked.append((same_event, similarity, market_id))
    ranked.sort(key=lambda row: (-row[0], -row[1], row[2]))
    controls = tuple(row[2] for row in ranked[: max(0, int(maximum_controls))])
    if len(controls) < int(minimum_controls):
        return None
    return TargetControlPlan(str(target_id), controls)


def build_predeclared_target_panel(
    histories: Mapping[str, Mapping[int, float]],
    plan: TargetControlPlan,
    *,
    bucket_seconds: int,
    min_points: int,
) -> core.RawPanel | None:
    ids = [plan.target, *plan.controls]
    # No post-history control substitution is permitted.
    if any(market_id not in histories for market_id in ids):
        return None
    return core.build_raw_panel(histories, ids, bucket_seconds=bucket_seconds, min_points=min_points)


def _model_common_and_residual(
    panel: core.RawPanel,
    model: core.PcaTargetModel,
) -> tuple[list[float], list[float], list[float]] | None:
    if model.target not in panel.values or any(control not in panel.values for control in model.controls):
        return None
    target_raw = list(panel.values[model.target])
    target_std = [(value - model.target_mean) / model.target_scale for value in target_raw]
    controls_std: list[list[float]] = []
    for control, mean, scale in zip(model.controls, model.control_means, model.control_scales):
        if scale <= 1e-12:
            return None
        controls_std.append([(value - mean) / scale for value in panel.values[control]])
    common: list[float] = []
    residual: list[float] = []
    for index in range(len(panel.times)):
        current_controls = [controls_std[j][index] for j in range(len(model.controls))]
        factors = [core.dot(vector, current_controls) for vector in model.eigenvectors]
        common_value = core.dot(model.beta, factors)
        common.append(common_value)
        residual.append(target_std[index] - common_value)
    return target_std, common, residual


def conditional_null_panel(
    panel: core.RawPanel,
    observed: core.PcaTargetModel,
    rng: random.Random,
) -> core.RawPanel | None:
    """Generate the target-residual I(1) null conditional on observed controls."""
    parts = _model_common_and_residual(panel, observed)
    if parts is None:
        return None
    _target_std, common, residual = parts
    points = len(residual)
    if points < 12:
        return None
    increments = [residual[index] - residual[index - 1] for index in range(1, points)]
    drift = statistics.fmean(increments)
    centered = [value - drift for value in increments]
    block = max(2, min(points - 1, int(round(math.sqrt(points - 1)))))
    indices = core._block_indices(points - 1, block, rng)
    null_residual = [residual[0]]
    for index in indices:
        null_residual.append(null_residual[-1] + drift + centered[index])
    null_target = tuple(
        observed.target_mean + observed.target_scale * (common[index] + null_residual[index])
        for index in range(points)
    )
    values = dict(panel.values)
    values[observed.target] = null_target
    # The nuisance control path is copied exactly, preserving stationary/mixed-order
    # controls instead of silently changing all controls into random walks.
    return core.RawPanel(panel.times, values)


def conditional_target_bootstrap_pvalue(
    panel: core.RawPanel,
    target: str,
    *,
    reps: int = 300,
    seed: int = 20260826,
    max_components: int = 3,
    explained_variance_threshold: float = 0.80,
    ridge: float = 1e-4,
) -> tuple[core.PcaTargetModel, float] | None:
    observed = core.fit_target(
        panel,
        target,
        max_components=max_components,
        explained_variance_threshold=explained_variance_threshold,
        ridge=ridge,
    )
    if observed is None:
        return None
    total = max(50, int(reps))
    rng = random.Random(seed)
    left = 0
    valid = 0
    for _ in range(total):
        boot = conditional_null_panel(panel, observed, rng)
        if boot is None:
            continue
        model = core.fit_target(
            boot,
            target,
            max_components=max_components,
            explained_variance_threshold=explained_variance_threshold,
            ridge=ridge,
        )
        if model is None:
            continue
        valid += 1
        if model.adf_t <= observed.adf_t:
            left += 1
    if valid < max(25, total // 2):
        return None
    return observed, (left + 1.0) / (valid + 1.0)


def benjamini_yekutieli_selected(pvalues: Mapping[str, float], q: float) -> set[str]:
    """Dependence-robust FDR control over the full predeclared family."""
    ordered = sorted((float(p), str(key)) for key, p in pvalues.items() if math.isfinite(float(p)))
    m = len(ordered)
    if m == 0:
        return set()
    harmonic = sum(1.0 / index for index in range(1, m + 1))
    effective_q = core.clamp(float(q), 1e-8, 0.5) / harmonic
    cutoff = 0.0
    for index, (pvalue, _key) in enumerate(ordered, start=1):
        if pvalue <= effective_q * index / m:
            cutoff = pvalue
    return {key for pvalue, key in ordered if cutoff > 0.0 and pvalue <= cutoff}


def by_effective_q(family_size: int, q: float) -> float:
    if family_size <= 0:
        return 0.0
    harmonic = sum(1.0 / index for index in range(1, family_size + 1))
    return float(q) / harmonic


def total_single_leg_sigma_logit(
    panel: core.RawPanel,
    model: core.PcaTargetModel,
    horizon_steps: int,
) -> float:
    """Historical total forecast-error sigma for the unhedged single target.

    The PCA signal forecasts residual mean reversion while leaving the common factor
    unhedged.  The uncertainty therefore includes both the common-component move and
    the residual AR forecast error, including their empirical covariance.
    """
    parts = _model_common_and_residual(panel, model)
    if parts is None:
        return math.inf
    _target_std, common, residual = parts
    steps = max(1, int(horizon_steps))
    if len(residual) <= steps + 5 or not 0.0 < model.phi < 0.999:
        return math.inf
    errors: list[float] = []
    phi_h = model.phi ** steps
    for start in range(0, len(residual) - steps):
        end = start + steps
        common_error = common[end] - common[start]
        residual_forecast = model.residual_mean + phi_h * (residual[start] - model.residual_mean)
        residual_error = residual[end] - residual_forecast
        errors.append((common_error + residual_error) * model.target_scale)
    sigma = core.stdev(errors)
    return max(1e-6, sigma) if math.isfinite(sigma) and sigma > 0.0 else math.inf


def score_with_total_single_leg_risk(
    panel: core.RawPanel,
    model: core.PcaTargetModel,
    current_logits: Mapping[str, float],
    horizon_steps: int,
) -> core.PcaScore | None:
    score = core.score_current(model, current_logits, horizon_steps)
    if score is None:
        return None
    total_sigma = total_single_leg_sigma_logit(panel, model, horizon_steps)
    if not math.isfinite(total_sigma):
        return None
    # Never let the full single-leg uncertainty fall below the residual-only sigma.
    return replace(score, sigma_logit=max(score.sigma_logit, total_sigma))
