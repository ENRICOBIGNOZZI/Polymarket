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
    residual_a = tuple(y - loading_a * f for y, f in zip(panel.values[market_a], factor))
    residual_b = tuple(y - loading_b * f for y, f in zip(panel.values[market_b], factor))
    phi_a, mu_a, sd_a = core_base.ar1_fit(residual_a)
    phi_b, mu_b, sd_b = core_base.ar1_fit(residual_b)
    if sd_a <= 1e-8 or sd_b <= 1e-8:
        return None
    adf_a = core_base.adf_t_stat(residual_a)
    adf_b = core_base.adf_t_stat(residual_b)
    return core_base.PairFit(
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
    )


core_base.fit_pair = fit_pair
for _name in dir(core_base):
    if not _name.startswith("__") and _name != "fit_pair":
        globals()[_name] = getattr(core_base, _name)
globals()["fit_pair"] = fit_pair
globals()["orientation_invariant_pc1"] = orientation.orientation_invariant_pc1
globals()["PanelFreshness"] = PanelFreshness
globals()["completed_history_view"] = completed_history_view
globals()["assess_panel_freshness"] = assess_panel_freshness
