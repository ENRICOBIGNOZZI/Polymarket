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


primitives = _load("v7_pca_stat_arb_primitives_runtime", "v7_pca_stat_arb_primitives.py")
_primitive_fit_target = primitives.fit_target


@dataclass(frozen=True)
class CurrentPcaTargetModel(primitives.PcaTargetModel):
    factor_phis: tuple[float, ...] = ()
    factor_means: tuple[float, ...] = ()
    factor_sds: tuple[float, ...] = ()
    factor_innovation_sds: tuple[float, ...] = ()
    factor_last: tuple[float, ...] = ()


@dataclass(frozen=True)
class CurrentPcaScore(primitives.PcaScore):
    predicted_common_logit_move: float = 0.0
    predicted_residual_logit_move: float = 0.0
    common_factor_forecast_identified: bool = False


def _factor_histories(panel, model: primitives.PcaTargetModel) -> list[list[float]] | None:
    if any(control not in panel.values for control in model.controls):
        return None
    controls_std: list[list[float]] = []
    for control, mean, scale in zip(model.controls, model.control_means, model.control_scales):
        if scale <= 1e-12:
            return None
        controls_std.append([(float(value) - mean) / scale for value in panel.values[control]])
    factors: list[list[float]] = [[] for _ in model.eigenvectors]
    for index in range(len(panel.times)):
        current_controls = [controls_std[j][index] for j in range(len(model.controls))]
        for factor_index, vector in enumerate(model.eigenvectors):
            factors[factor_index].append(primitives.dot(vector, current_controls))
    return factors


def fit_target(
    panel,
    target: str,
    max_components: int = 3,
    explained_variance_threshold: float = 0.80,
    ridge: float = 1e-4,
):
    fitted = _primitive_fit_target(
        panel,
        target,
        max_components=max_components,
        explained_variance_threshold=explained_variance_threshold,
        ridge=ridge,
    )
    if fitted is None:
        return None
    histories = _factor_histories(panel, fitted)
    if histories is None or len(histories) != len(fitted.beta):
        return None
    phis: list[float] = []
    means: list[float] = []
    sds: list[float] = []
    innovations: list[float] = []
    last: list[float] = []
    for series in histories:
        phi, mean, sd, innovation_sd = primitives.ar1_fit(series)
        if not all(math.isfinite(value) for value in (phi, mean, sd, innovation_sd)):
            return None
        phis.append(float(phi))
        means.append(float(mean))
        sds.append(float(sd))
        innovations.append(float(innovation_sd))
        last.append(float(series[-1]))
    return CurrentPcaTargetModel(
        **fitted.__dict__,
        factor_phis=tuple(phis),
        factor_means=tuple(means),
        factor_sds=tuple(sds),
        factor_innovation_sds=tuple(innovations),
        factor_last=tuple(last),
    )


def score_current(model: CurrentPcaTargetModel, current_logits: Mapping[str, float], horizon_steps: int):
    if not isinstance(model, CurrentPcaTargetModel):
        return None
    if model.target not in current_logits or any(mid not in current_logits for mid in model.controls):
        return None
    if not 0.0 < model.phi < 0.999:
        return None
    if not (
        len(model.factor_phis)
        == len(model.factor_means)
        == len(model.factor_innovation_sds)
        == len(model.beta)
        == len(model.eigenvectors)
    ):
        return None
    controls_std: list[float] = []
    for mid, mean, scale in zip(model.controls, model.control_means, model.control_scales):
        if scale <= 1e-12:
            return None
        value = float(current_logits[mid])
        if not math.isfinite(value):
            return None
        controls_std.append((value - mean) / scale)
    factors = [primitives.dot(vector, controls_std) for vector in model.eigenvectors]
    if not all(math.isfinite(value) for value in factors):
        return None
    target_value = float(current_logits[model.target])
    if not math.isfinite(target_value):
        return None
    target_std = (target_value - model.target_mean) / model.target_scale
    common_current = primitives.dot(model.beta, factors)
    residual = target_std - common_current
    steps = max(1, int(horizon_steps))

    factor_moves: list[float] = []
    for current, phi, mean, innovation_sd in zip(
        factors,
        model.factor_phis,
        model.factor_means,
        model.factor_innovation_sds,
    ):
        # Single-leg PCA is deliberately fail-closed when the common-factor
        # conditional mean is not identified by a stable AR(1) law.
        if not (-0.999 < phi < 0.999) or innovation_sd <= 1e-8:
            return None
        factor_moves.append((phi ** steps - 1.0) * (current - mean))

    common_move_std = primitives.dot(model.beta, factor_moves)
    residual_move_std = (model.phi ** steps - 1.0) * (residual - model.residual_mean)
    common_move = common_move_std * model.target_scale
    residual_move = residual_move_std * model.target_scale
    predicted_logit_move = common_move + residual_move

    residual_variance_multiplier = sum(model.phi ** (2 * j) for j in range(steps))
    residual_variance = model.innovation_sd ** 2 * max(1.0, residual_variance_multiplier)
    factor_variance = 0.0
    for beta, phi, innovation_sd in zip(model.beta, model.factor_phis, model.factor_innovation_sds):
        multiplier = sum(phi ** (2 * j) for j in range(steps))
        factor_variance += beta * beta * innovation_sd * innovation_sd * max(1.0, multiplier)
    sigma_logit = math.sqrt(max(1e-12, residual_variance + factor_variance)) * model.target_scale
    return CurrentPcaScore(
        target=model.target,
        current_probability=primitives.logistic(current_logits[model.target]),
        current_residual=residual,
        residual_z=(residual - model.residual_mean) / model.residual_sd,
        predicted_logit_move=predicted_logit_move,
        sigma_logit=max(1e-6, sigma_logit),
        horizon_steps=steps,
        predicted_common_logit_move=common_move,
        predicted_residual_logit_move=residual_move,
        common_factor_forecast_identified=True,
    )


primitives.fit_target = fit_target
primitives.score_current = score_current
for _name in dir(primitives):
    if not _name.startswith("__") and _name not in {"fit_target", "score_current", "PcaTargetModel", "PcaScore"}:
        globals()[_name] = getattr(primitives, _name)

globals()["PcaTargetModel"] = CurrentPcaTargetModel
globals()["PcaScore"] = CurrentPcaScore
globals()["fit_target"] = fit_target
globals()["score_current"] = score_current
