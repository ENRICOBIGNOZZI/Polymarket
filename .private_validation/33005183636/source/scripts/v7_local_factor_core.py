#!/usr/bin/env python3
from __future__ import annotations

import importlib.util
import sys
from pathlib import Path
from typing import Any


def _load(name: str, filename: str) -> Any:
    path = Path(__file__).with_name(filename)
    spec = importlib.util.spec_from_file_location(name, path)
    if spec is None or spec.loader is None:
        raise ImportError(f"cannot load {path}")
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


legacy = _load("v7_local_factor_core_mean_legacy_runtime", "v7_local_factor_core_mean_legacy.py")
orientation = _load("v7_local_factor_orientation_runtime", "v7_local_factor_orientation.py")


def fit_pair(panel, market_a: str, market_b: str, min_controls: int = 2):
    """Fit both targets to one pair-excluded, orientation-invariant temporal PC1."""
    if market_a == market_b or market_a not in panel.values or market_b not in panel.values:
        return None
    controls = tuple(sorted(mid for mid in panel.values if mid not in {market_a, market_b}))
    if len(controls) < min_controls:
        return None
    factor = orientation.orientation_invariant_pc1({mid: panel.values[mid] for mid in controls})
    if factor is None:
        return None
    loading_a = legacy.ols_loading(panel.values[market_a], factor)
    loading_b = legacy.ols_loading(panel.values[market_b], factor)
    if loading_a is None or loading_b is None:
        return None
    residual_a = tuple(y - loading_a * f for y, f in zip(panel.values[market_a], factor))
    residual_b = tuple(y - loading_b * f for y, f in zip(panel.values[market_b], factor))
    phi_a, mu_a, sd_a = legacy.ar1_fit(residual_a)
    phi_b, mu_b, sd_b = legacy.ar1_fit(residual_b)
    if sd_a <= 1e-8 or sd_b <= 1e-8:
        return None
    adf_a = legacy.adf_t_stat(residual_a)
    adf_b = legacy.adf_t_stat(residual_b)
    return legacy.PairFit(
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


# Bootstrap/inference functions defined in the frozen module resolve fit_pair in
# that module's global namespace. Patching it makes every bootstrap replicate
# re-estimate the orientation-invariant factor instead of silently reverting to
# the old standardized-control mean.
legacy.fit_pair = fit_pair

for _name in dir(legacy):
    if not _name.startswith("__") and _name != "fit_pair":
        globals()[_name] = getattr(legacy, _name)
globals()["fit_pair"] = fit_pair
globals()["orientation_invariant_pc1"] = orientation.orientation_invariant_pc1
