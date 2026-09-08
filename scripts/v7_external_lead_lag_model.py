#!/usr/bin/env python3
"""Frozen external-feature model for short-horizon Polymarket repricing."""
from __future__ import annotations
import math
from typing import Any
from v7_external_rich_model import FEATURE_NAMES, design, number, logit, sigmoid

SCHEMA = "polymarket_v7_external_pm_lead_lag_model_v1"
FAMILY = "btc_m5_external_pm_lead_lag_ridge_v1"


def validate(model: dict[str, Any]) -> None:
    if model.get("schema") != SCHEMA or model.get("family") != FAMILY:
        raise ValueError("lead_lag:model_schema")
    if model.get("paper_only") is not True or model.get("research_only") is not True:
        raise ValueError("lead_lag:authority")
    if model.get("real_order_submission") is not False:
        raise ValueError("lead_lag:real_order_submission")
    if model.get("training_lifecycle") != "EXPLICIT_FROZEN_ARTIFACT_ONLY":
        raise ValueError("lead_lag:lifecycle")
    for horizon, spec in (model.get("models") or {}).items():
        if int(horizon) not in (100, 250, 500, 1000) or not isinstance(spec, dict):
            raise ValueError("lead_lag:horizon")
        names = spec.get("feature_names")
        if not isinstance(names, list) or not names or any(n not in FEATURE_NAMES for n in names):
            raise ValueError("lead_lag:features")
        p = {"feature_names": names, "means": spec.get("means"), "scales": spec.get("scales")}
        if not isinstance(p["means"], list) or not isinstance(p["scales"], list):
            raise ValueError("lead_lag:normalization")
        if len(p["means"]) != len(names) or len(p["scales"]) != len(names):
            raise ValueError("lead_lag:normalization_shape")
        if any(number(v) is None or float(v) <= 0 for v in p["scales"]):
            raise ValueError("lead_lag:scale")
        coeff = spec.get("coefficients")
        if not isinstance(coeff, list) or len(coeff) != 1 + 2 * len(names):
            raise ValueError("lead_lag:coefficients")
        if any(number(v) is None for v in coeff):
            raise ValueError("lead_lag:nonfinite")


def predict_delta_logit(model: dict[str, Any], features: dict[str, Any], horizon_ms: int) -> float:
    validate(model)
    spec = (model.get("models") or {}).get(str(int(horizon_ms)))
    if not isinstance(spec, dict):
        raise ValueError("lead_lag:horizon_unavailable")
    params = {"feature_names": spec["feature_names"], "means": spec["means"], "scales": spec["scales"]}
    x = design(features, params)
    value = sum(float(a) * float(b) for a, b in zip(spec["coefficients"], x))
    cap = float(spec.get("maximum_absolute_delta_logit", 2.0))
    return max(-cap, min(cap, value))


def predict_probability(model: dict[str, Any], features: dict[str, Any], market_probability: float,
                        horizon_ms: int) -> dict[str, float]:
    p0 = number(market_probability)
    if p0 is None or not 0 <= p0 <= 1:
        raise ValueError("lead_lag:market_probability")
    delta = predict_delta_logit(model, features, horizon_ms)
    p1 = sigmoid(logit(p0) + delta)
    return {"market_probability": p0, "predicted_probability": p1,
            "predicted_delta_probability": p1 - p0, "predicted_delta_logit": delta}
