#!/usr/bin/env python3
"""Shared pure functions for frozen PM repricing research inference."""
from __future__ import annotations

import hashlib
import json
import math
import os
from pathlib import Path
from typing import Any

from v7_external_rich_model import logit
from v7_pm_repricing_incremental_benchmark import predict

ARTIFACT_SCHEMA = "polymarket_v7_pm_repricing_incremental_benchmark_v1"
FROZEN_ARTIFACT_SCHEMA = "polymarket_v7_pm_repricing_shadow_artifact_v1"


def atomic_json(path: Path, value: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + f".tmp.{os.getpid()}")
    tmp.write_text(json.dumps(value, sort_keys=True) + "\n", encoding="utf-8")
    os.replace(tmp, path)


def exact_hex(value: str, length: int) -> bool:
    return len(value) == length and all(ch in "0123456789abcdef" for ch in value)


def file_sha256(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def load_model(path: Path, expected_sha256: str, horizon_ms: int, family: str) -> tuple[dict[str, Any], dict[str, Any]]:
    if not exact_hex(expected_sha256, 64) or file_sha256(path) != expected_sha256:
        raise ValueError("repricing_shadow:artifact_hash_mismatch")
    value = json.loads(path.read_text(encoding="utf-8"))
    if (
        not isinstance(value, dict)
        or value.get("schema") not in {ARTIFACT_SCHEMA, FROZEN_ARTIFACT_SCHEMA}
        or value.get("paper_only") is not True
        or value.get("authenticated_execution") is not False
        or value.get("real_order_submission") is not False
        or value.get("execution_authority") != "ZERO_AUTHORITY_RESEARCH_ONLY"
        or value.get("automatic_promotion") is not False
    ):
        raise ValueError("repricing_shadow:artifact_safety_contract")
    code_sha = str(value.get("code_sha") or value.get("source_code_sha") or "")
    if not exact_hex(code_sha, 40):
        raise ValueError("repricing_shadow:artifact_code_sha_missing")
    if value.get("schema") == FROZEN_ARTIFACT_SCHEMA:
        source_sha = str(value.get("source_artifact_sha256") or "")
        if not exact_hex(source_sha, 64):
            raise ValueError("repricing_shadow:source_artifact_hash_missing")
    models = value.get("models") if isinstance(value.get("models"), dict) else {}
    by_horizon = models.get(str(horizon_ms)) if isinstance(models.get(str(horizon_ms)), dict) else {}
    spec = by_horizon.get(family) if isinstance(by_horizon.get(family), dict) else None
    if spec is None or spec.get("family") != family:
        raise ValueError("repricing_shadow:model_missing")
    coefficients = spec.get("coefficients")
    names = spec.get("feature_names")
    if not isinstance(coefficients, list) or not isinstance(names, list) or len(coefficients) != 1 + 2 * len(names):
        raise ValueError("repricing_shadow:model_shape")
    return value, spec


def logistic(value: float) -> float:
    if value >= 0:
        z = math.exp(-min(value, 60.0))
        return 1.0 / (1.0 + z)
    z = math.exp(max(value, -60.0))
    return z / (1.0 + z)


def token_tick(cuts: list[dict[str, Any]], token_id: str) -> float | None:
    for row in cuts:
        if isinstance(row, dict) and str(row.get("token_id") or "") == token_id:
            try:
                value = float(row.get("tick_size"))
            except (TypeError, ValueError, OverflowError):
                return None
            return value if math.isfinite(value) and 0 < value < 1 else None
    return None


def score_origin(origin: dict[str, Any], evidence: dict[str, Any], spec: dict[str, Any], *,
                 artifact_sha256: str, artifact_code_sha: str, runtime_sha: str,
                 horizon_ms: int, family: str, threshold_ticks: float,
                 scored_wall_ns: int) -> dict[str, Any]:
    cuts = evidence.get("origin_book_cuts")
    if not isinstance(cuts, list) or len(cuts) != 2:
        raise ValueError("repricing_shadow:origin_book_cut_missing")
    row = {**origin, **evidence}
    prediction = predict(row, spec)
    if prediction is None or not math.isfinite(float(prediction)):
        raise ValueError("repricing_shadow:prediction_missing")
    p0 = float(evidence["origin_pm_yes"])
    p1 = logistic(logit(p0) + float(prediction))
    yes_tick = token_tick(cuts, str(origin.get("yes_token") or ""))
    no_tick = token_tick(cuts, str(origin.get("no_token") or ""))
    if yes_tick is None or no_tick is None:
        raise ValueError("repricing_shadow:tick_missing")
    delta = p1 - p0
    adverse_yes_ticks = max(0.0, -delta / yes_tick)
    adverse_no_ticks = max(0.0, delta / no_tick)
    return {
        "schema": "polymarket_v7_pm_repricing_shadow_v1",
        "paper_only": True,
        "authenticated_execution": False,
        "real_order_submission": False,
        "real_money_authority": False,
        "research_only": True,
        "execution_authority": "ZERO_AUTHORITY_RESEARCH_ONLY",
        "runtime_model_sha": runtime_sha,
        "artifact_sha256": artifact_sha256,
        "artifact_code_sha": artifact_code_sha,
        "family": family,
        "horizon_ms": horizon_ms,
        "threshold_ticks": threshold_ticks,
        "market_id": origin["market_id"],
        "origin_id": origin["origin_id"],
        "origin_observed_wall_ns": int(origin["origin_observed_wall_ns"]),
        "scored_wall_ns": scored_wall_ns,
        "inference_age_ns": max(0, scored_wall_ns - int(origin["origin_observed_wall_ns"])),
        "origin_pm_yes": p0,
        "predicted_pm_yes": p1,
        "predicted_delta_logit": float(prediction),
        "predicted_delta_probability": delta,
        "yes_tick_size": yes_tick,
        "no_tick_size": no_tick,
        "adverse_yes_ticks": adverse_yes_ticks,
        "adverse_no_ticks": adverse_no_ticks,
        "would_veto_yes_buy": adverse_yes_ticks + 1e-12 >= threshold_ticks,
        "would_veto_no_buy": adverse_no_ticks + 1e-12 >= threshold_ticks,
        "origin_pm_snapshot_id": evidence.get("origin_pm_snapshot_id"),
        "observer_session_id": evidence.get("observer_session_id"),
        "connection_epoch": evidence.get("connection_epoch"),
        "rich_feature_sha256": origin.get("rich_feature_sha256"),
        "feature_schema_version": origin.get("feature_schema_version"),
    }
