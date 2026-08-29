#!/usr/bin/env python3
from __future__ import annotations

import importlib.util
import math
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Mapping


def _load(name: str, filename: str) -> Any:
    path = Path(__file__).with_name(filename)
    spec = importlib.util.spec_from_file_location(name, path)
    if spec is None or spec.loader is None:
        raise ImportError(f"cannot load {path}")
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


core_base = _load("v7_local_factor_core_base_runtime", "v7_local_factor_core_base.py")
orientation = _load("v7_local_factor_orientation_runtime", "v7_local_factor_orientation.py")


@dataclass(frozen=True)
class PanelFreshness:
    latest_bucket_start_ts: int | None
    latest_completed_bucket_end_ts: int | None
    state_age_seconds: int | None
    maximum_state_age_seconds: int
    current_bucket_start_ts: int
    fresh: bool
    reason: str


@dataclass(frozen=True)
class CurrentPairFit(core_base.PairFit):
    """Frozen LF fit plus the nuisance projection needed for a causal current score."""

    control_factor_loadings: tuple[tuple[str, float], ...] = ()
    standardization_means: tuple[tuple[str, float], ...] = ()
    standardization_scales: tuple[tuple[str, float], ...] = ()


@dataclass(frozen=True)
class CurrentResidualState:
    factor: float
    residual_a: float
    residual_b: float
    residual_z_a: float
    residual_z_b: float


@dataclass(frozen=True)
class CurrentPairSignal(core_base.PairSignal):
    current_factor: float = 0.0
    current_residual_a: float = 0.0
    current_residual_b: float = 0.0
    current_residual_z_a: float = 0.0
    current_residual_z_b: float = 0.0


def completed_history_view(
    histories: Mapping[str, Mapping[int, float]],
    *,
    now: int,
    bucket_seconds: int,
) -> dict[str, dict[int, float]]:
    bucket = int(bucket_seconds)
    if bucket <= 0:
        raise ValueError("bucket_seconds must be positive")
    decision_ts = int(now)
    current_bucket_start = (decision_ts // bucket) * bucket
    out: dict[str, dict[int, float]] = {}
    for market_id, series in histories.items():
        completed: dict[int, float] = {}
        for raw_ts, value in series.items():
            ts = int(raw_ts)
            if ts < current_bucket_start:
                completed[ts] = float(value)
        if completed:
            out[str(market_id)] = completed
    return out


def assess_panel_freshness(
    panel: Any,
    *,
    now: int,
    bucket_seconds: int,
    maximum_age_buckets: float = 2.0,
) -> PanelFreshness:
    bucket = int(bucket_seconds)
    decision_ts = int(now)
    max_buckets = float(maximum_age_buckets)
    if bucket <= 0 or not math.isfinite(max_buckets) or max_buckets < 0.0:
        raise ValueError("invalid Local Factor freshness contract")
    current_bucket_start = (decision_ts // bucket) * bucket
    maximum_age_seconds = int(max_buckets * bucket)
    times = tuple(int(t) for t in getattr(panel, "times", ()))
    if not times:
        return PanelFreshness(None, None, None, maximum_age_seconds, current_bucket_start, False, "missing_panel_times")
    if any(b - a != bucket for a, b in zip(times, times[1:])):
        return PanelFreshness(times[-1], None, None, maximum_age_seconds, current_bucket_start, False, "irregular_panel_times")
    latest_start = times[-1]
    if latest_start >= current_bucket_start:
        return PanelFreshness(latest_start, latest_start + bucket, None, maximum_age_seconds, current_bucket_start, False, "incomplete_or_future_bucket")
    latest_end = latest_start + bucket
    age = decision_ts - latest_end
    if age < 0:
        return PanelFreshness(latest_start, latest_end, age, maximum_age_seconds, current_bucket_start, False, "future_history_state")
    if age > maximum_age_seconds:
        return PanelFreshness(latest_start, latest_end, age, maximum_age_seconds, current_bucket_start, False, "stale_history_state")
    return PanelFreshness(latest_start, latest_end, age, maximum_age_seconds, current_bucket_start, True, "fresh_completed_regular_history")


def fit_pair(panel, market_a: str, market_b: str, min_controls: int = 2):
    if market_a == market_b or market_a not in panel.values or market_b not in panel.values:
        return None
    controls = tuple(sorted(mid for mid in panel.values if mid not in {market_a, market_b}))
    if len(controls) < min_controls:
        return None
    factor = orientation.orientation_invariant_pc1({mid: panel.values[mid] for mid in controls})
    if factor is None:
        return None
    loading_a = core_base.ols_loading(panel.values[market_a], factor)
    loading_b = core_base.ols_loading(panel.values[market_b], factor)
    if loading_a is None or loading_b is None:
        return None
    control_loadings: list[tuple[str, float]] = []
    for control in controls:
        loading = core_base.ols_loading(panel.values[control], factor)
        if loading is None or not math.isfinite(loading):
            return None
        control_loadings.append((control, float(loading)))
    denominator = sum(loading * loading for _control, loading in control_loadings)
    if denominator <= 1e-12:
        return None
    residual_a = tuple(y - loading_a * f for y, f in zip(panel.values[market_a], factor))
    residual_b = tuple(y - loading_b * f for y, f in zip(panel.values[market_b], factor))
    phi_a, mu_a, sd_a = core_base.ar1_fit(residual_a)
    phi_b, mu_b, sd_b = core_base.ar1_fit(residual_b)
    if sd_a <= 1e-8 or sd_b <= 1e-8:
        return None
    adf_a = core_base.adf_t_stat(residual_a)
    adf_b = core_base.adf_t_stat(residual_b)
    required = (market_a, market_b, *controls)
    if any(mid not in panel.means or mid not in panel.scales for mid in required):
        return None
    return CurrentPairFit(
        market_a=market_a,
        market_b=market_b,
        controls=controls,
        loading_a=loading_a,
        loading_b=loading_b,
        residual_a=residual_a,
        residual_b=residual_b,
        phi_a=phi_a,
        phi_b=phi_b,
        residual_mean_a=mu_a,
        residual_mean_b=mu_b,
        residual_sd_a=sd_a,
        residual_sd_b=sd_b,
        residual_z_a=(residual_a[-1] - mu_a) / sd_a,
        residual_z_b=(residual_b[-1] - mu_b) / sd_b,
        adf_a=adf_a,
        adf_b=adf_b,
        pair_stat=max(adf_a, adf_b),
        control_factor_loadings=tuple(control_loadings),
        standardization_means=tuple((mid, float(panel.means[mid])) for mid in required),
        standardization_scales=tuple((mid, float(panel.scales[mid])) for mid in required),
    )


def current_residual_state(fit: CurrentPairFit, probabilities: Mapping[str, float]) -> CurrentResidualState | None:
    if not isinstance(fit, CurrentPairFit) or not fit.control_factor_loadings:
        return None
    required = (fit.market_a, fit.market_b, *fit.controls)
    if any(mid not in probabilities for mid in required):
        return None
    means = dict(fit.standardization_means)
    scales = dict(fit.standardization_scales)
    if any(mid not in means or mid not in scales or scales[mid] <= 1e-12 for mid in required):
        return None
    z: dict[str, float] = {}
    for mid in required:
        probability = float(probabilities[mid])
        if not math.isfinite(probability) or not 0.0 < probability < 1.0:
            return None
        z[mid] = (core_base.logit(probability) - means[mid]) / scales[mid]
    numerator = 0.0
    denominator = 0.0
    for control, loading in fit.control_factor_loadings:
        if control not in z or not math.isfinite(loading):
            return None
        numerator += loading * z[control]
        denominator += loading * loading
    if denominator <= 1e-12:
        return None
    factor = numerator / denominator
    residual_a = z[fit.market_a] - fit.loading_a * factor
    residual_b = z[fit.market_b] - fit.loading_b * factor
    return CurrentResidualState(
        factor=factor,
        residual_a=residual_a,
        residual_b=residual_b,
        residual_z_a=(residual_a - fit.residual_mean_a) / fit.residual_sd_a,
        residual_z_b=(residual_b - fit.residual_mean_b) / fit.residual_sd_b,
    )


def build_pair_signal(
    fit: CurrentPairFit,
    pvalue: float,
    probabilities: Mapping[str, float],
    yes_scales: Mapping[str, float],
    bucket_seconds: int,
    now: int,
    end_ts: Mapping[str, int | None] | None = None,
    resolution_ts: Mapping[str, int | None] | None = None,
    exit_buffer_seconds: int = 0,
    min_abs_z: float = 0.75,
    max_hold_seconds: int = 48 * 3600,
    min_weight: float = 0.05,
    max_weight: float = 10.0,
) -> CurrentPairSignal | None:
    state = current_residual_state(fit, probabilities)
    if state is None:
        return None
    if abs(state.residual_z_a) < min_abs_z or abs(state.residual_z_b) < min_abs_z:
        return None
    if not (0.0 < fit.phi_a < 1.0 and 0.0 < fit.phi_b < 1.0):
        return None
    if fit.market_a not in yes_scales or fit.market_b not in yes_scales:
        return None
    hl = max(core_base.half_life_bars(fit.phi_a), core_base.half_life_bars(fit.phi_b))
    hold = max(bucket_seconds, min(max_hold_seconds, int(math.ceil(hl * bucket_seconds))))
    resolved = end_ts if end_ts is not None else resolution_ts
    if resolved is None:
        return None
    raw_a = resolved.get(fit.market_a)
    raw_b = resolved.get(fit.market_b)
    if raw_a is None or raw_b is None:
        return None
    ttr = min(int(raw_a), int(raw_b)) - int(now) - max(0, int(exit_buffer_seconds))
    if ttr <= 0:
        return None
    hold = min(hold, ttr)
    steps = max(1.0, hold / max(1, int(bucket_seconds)))
    change_a = core_base.residual_change(fit.phi_a, state.residual_a, fit.residual_mean_a, steps)
    change_b = core_base.residual_change(fit.phi_b, state.residual_b, fit.residual_mean_b, steps)
    if abs(change_a) <= 1e-12 or abs(change_b) <= 1e-12:
        return None
    side_a = "YES" if change_a > 0 else "NO"
    side_b = "YES" if change_b > 0 else "NO"
    p_a = float(probabilities[fit.market_a])
    p_b = float(probabilities[fit.market_b])
    exposure_a = core_base.price_factor_exposure(side_a, p_a, float(yes_scales[fit.market_a]), fit.loading_a)
    exposure_b = core_base.price_factor_exposure(side_b, p_b, float(yes_scales[fit.market_b]), fit.loading_b)
    if abs(exposure_a) <= 1e-12 or abs(exposure_b) <= 1e-12 or exposure_a * exposure_b >= 0:
        return None
    lo = max(1e-6, float(min_weight))
    hi = max(lo, float(max_weight))
    ratio = core_base.clamp(abs(exposure_a / exposure_b), lo, hi)
    weight_a = 1.0
    weight_b = ratio
    return CurrentPairSignal(
        market_a=fit.market_a,
        market_b=fit.market_b,
        side_a=side_a,
        side_b=side_b,
        weight_a=weight_a,
        weight_b=weight_b,
        hold_seconds=hold,
        residual_change_a=change_a,
        residual_change_b=change_b,
        factor_exposure_a=exposure_a * weight_a,
        factor_exposure_b=exposure_b * weight_b,
        pvalue=float(pvalue),
        current_factor=state.factor,
        current_residual_a=state.residual_a,
        current_residual_b=state.residual_b,
        current_residual_z_a=state.residual_z_a,
        current_residual_z_b=state.residual_z_b,
    )


core_base.fit_pair = fit_pair
for _name in dir(core_base):
    if not _name.startswith("__") and _name not in {"fit_pair", "build_pair_signal", "PairFit"}:
        globals()[_name] = getattr(core_base, _name)

globals()["PairFit"] = CurrentPairFit
globals()["fit_pair"] = fit_pair
globals()["build_pair_signal"] = build_pair_signal
globals()["current_residual_state"] = current_residual_state
globals()["orientation_invariant_pc1"] = orientation.orientation_invariant_pc1
globals()["PanelFreshness"] = PanelFreshness
globals()["completed_history_view"] = completed_history_view
globals()["assess_panel_freshness"] = assess_panel_freshness
