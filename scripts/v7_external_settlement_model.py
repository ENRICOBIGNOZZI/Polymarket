#!/usr/bin/env python3
"""Inference contract for immutable BTC 5m settlement-margin artifacts."""
from __future__ import annotations

import math
from typing import Any

try:
    from v7_external_settlement_dataset import FEATURE_SCHEMA, MODEL_FEATURE_NAMES
    from v7_fair_value_registry import FairModelArtifact
except ModuleNotFoundError:
    from scripts.v7_external_settlement_dataset import FEATURE_SCHEMA, MODEL_FEATURE_NAMES
    from scripts.v7_fair_value_registry import FairModelArtifact


FAMILY = "btc_5m_settlement_margin_linear_v1"


def finite(value: Any, default: float | None = None) -> float | None:
    try:
        number = float(value)
    except (TypeError, ValueError, OverflowError):
        return default
    return number if math.isfinite(number) else default


def validate_parameters(artifact: FairModelArtifact) -> None:
    if artifact.family != FAMILY or artifact.feature_schema_version != FEATURE_SCHEMA:
        raise ValueError("settlement_model_family_or_schema")
    parameters = artifact.parameters
    names = parameters.get("feature_names")
    means = parameters.get("feature_means")
    scales = parameters.get("feature_scales")
    coefficients = parameters.get("coefficients")
    if names != list(MODEL_FEATURE_NAMES):
        raise ValueError("settlement_model_feature_order")
    if not all(isinstance(value, dict) for value in (means, scales, coefficients)):
        raise ValueError("settlement_model_parameter_maps")
    for name in names:
        mean = finite(means.get(name))
        scale = finite(scales.get(name))
        coefficient = finite(coefficients.get(name))
        if mean is None or scale is None or coefficient is None or scale <= 0.0:
            raise ValueError("settlement_model_parameter_invalid")
    if finite(parameters.get("intercept")) is None:
        raise ValueError("settlement_model_intercept")
    sigma = finite(parameters.get("default_residual_sigma_bps"))
    uncertainty = finite(parameters.get("mean_uncertainty_bps"))
    if sigma is None or uncertainty is None or sigma <= 0.0 or uncertainty < 0.0:
        raise ValueError("settlement_model_uncertainty")
    calibration = parameters.get("calibration")
    if not isinstance(calibration, dict):
        raise ValueError("settlement_model_calibration")
    intercept = finite(calibration.get("intercept"))
    slope = finite(calibration.get("slope"))
    if intercept is None or slope is None or not 0.05 <= slope <= 5.0:
        raise ValueError("settlement_model_calibration_invalid")


def runtime_features(
    *, tte_seconds: float, reference_price: float, oracle_price: float,
    external: dict[str, Any], oracle_age_ns: int,
) -> dict[str, float] | None:
    external_price = finite(external.get("composite_price"))
    if external_price is None:
        venues = external.get("venues") if isinstance(external.get("venues"), list) else []
        external_price = next((
            finite(row.get("price")) for row in venues if isinstance(row, dict)
            and finite(row.get("price")) is not None
        ), None)
    values = {
        "tte_seconds": finite(tte_seconds),
        "terminal_window_observed_fraction": max(0.0, min(1.0, (60.0 - tte_seconds) / 60.0)),
        "oracle_minus_reference_bps": (
            10_000.0 * (oracle_price / reference_price - 1.0)
            if min(reference_price, oracle_price) > 0.0 else None
        ),
        "external_minus_oracle_bps": (
            10_000.0 * (external_price / oracle_price - 1.0)
            if external_price is not None and min(external_price, oracle_price) > 0.0 else None
        ),
        "external_return_1s": finite(external.get("return_1s")),
        "external_return_5s": finite(external.get("return_5s")),
        "oracle_age_ms": max(0.0, oracle_age_ns / 1_000_000.0),
        "external_age_ms": max(0.0, (finite(external.get("age_ns"), 0.0) or 0.0) / 1_000_000.0),
    }
    if any(values.get(name) is None for name in MODEL_FEATURE_NAMES):
        return None
    return {name: float(values[name]) for name in MODEL_FEATURE_NAMES}


def _sigma_for_tte(parameters: dict[str, Any], tte: float) -> float:
    for bucket in parameters.get("residual_sigma_by_tte") if isinstance(
        parameters.get("residual_sigma_by_tte"), list) else []:
        if not isinstance(bucket, dict):
            continue
        minimum, maximum, sigma = (
            finite(bucket.get("minimum_seconds")), finite(bucket.get("maximum_seconds")),
            finite(bucket.get("sigma_bps")),
        )
        if None not in (minimum, maximum, sigma) and minimum <= tte <= maximum and sigma > 0.0:
            return sigma
    return float(parameters["default_residual_sigma_bps"])


def _logistic_calibrate(probability: float, calibration: dict[str, Any]) -> float:
    probability = min(1.0 - 1e-9, max(1e-9, probability))
    value = float(calibration["intercept"]) + float(calibration["slope"]) * math.log(
        probability / (1.0 - probability)
    )
    return 1.0 / (1.0 + math.exp(-value)) if value >= 0.0 else math.exp(value) / (1.0 + math.exp(value))


def predict(artifact: FairModelArtifact, features: dict[str, Any]) -> dict[str, float]:
    validate_parameters(artifact)
    parameters = artifact.parameters
    mean_margin = float(parameters["intercept"])
    for name in parameters["feature_names"]:
        value = finite(features.get(name))
        if value is None:
            raise ValueError(f"settlement_model_feature_missing:{name}")
        standardized = (value - float(parameters["feature_means"][name])) / float(
            parameters["feature_scales"][name]
        )
        mean_margin += float(parameters["coefficients"][name]) * standardized
    tte = float(features["tte_seconds"])
    sigma = _sigma_for_tte(parameters, tte)
    mean_uncertainty = float(parameters["mean_uncertainty_bps"])
    normal = lambda margin: 0.5 * math.erfc(-margin / sigma / math.sqrt(2.0))
    raw = min(1.0 - 1e-9, max(1e-9, normal(mean_margin)))
    lower_raw = min(1.0 - 1e-9, max(1e-9, normal(mean_margin - 1.64 * mean_uncertainty)))
    upper_raw = min(1.0 - 1e-9, max(1e-9, normal(mean_margin + 1.64 * mean_uncertainty)))
    calibration = parameters["calibration"]
    probability = _logistic_calibrate(raw, calibration)
    lower = _logistic_calibrate(lower_raw, calibration)
    upper = _logistic_calibrate(upper_raw, calibration)
    return {
        "yes": probability,
        "lower": min(lower, probability),
        "upper": max(upper, probability),
        "raw_yes": raw,
        "predicted_settlement_margin_bps": mean_margin,
        "settlement_sigma_bps": sigma,
        "mean_uncertainty_bps": mean_uncertainty,
    }
