#!/usr/bin/env python3
"""Freeze a cluster-weighted External Fair calibration challenger.

The challenger is trained only from settled SHADOW forecasts that predate its
publication. Publishing it grants zero execution authority; subsequent
contracts are its immutable forward OOS cohort.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import math
import os
import time
from collections import Counter
from pathlib import Path
from typing import Any, Iterable

from v7_fair_value_registry import FairModelArtifact, FairValueRegistry

MODEL_VERSION = "external-fair-structural-v7-paper"
EVIDENCE_SEMANTICS_VERSION = "external-fair-settlement-evidence-v1"


def finite(value: Any, default: float = math.nan) -> float:
    try:
        output = float(value)
    except (TypeError, ValueError, OverflowError):
        return default
    return output if math.isfinite(output) else default


def canonical_policy_hash(path: Path) -> str:
    value = json.loads(path.read_text(encoding="utf-8"))
    return hashlib.sha256(
        json.dumps(value, separators=(",", ":"), sort_keys=True).encode()
    ).hexdigest()


def records(paths: Iterable[Path]) -> dict[str, dict[str, Any]]:
    output: dict[str, dict[str, Any]] = {}
    for path in paths:
        try:
            handle = path.open(encoding="utf-8")
        except OSError:
            continue
        with handle:
            for line in handle:
                try:
                    row = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if not isinstance(row, dict):
                    continue
                identity = str(row.get("record_id") or "")
                if identity:
                    output.setdefault(identity, row)
    return output


def settled_rows(
    values: Iterable[dict[str, Any]], *, policy_sha256: str,
) -> list[dict[str, Any]]:
    forecasts: dict[str, dict[str, Any]] = {}
    finals: dict[str, dict[str, Any]] = {}
    for row in values:
        if (
            row.get("paper_only") is not True
            or row.get("authenticated_execution") is not False
            or row.get("real_order_submission") is not False
            or row.get("execution_authority") != "SHADOW_ZERO_AUTHORITY"
            or row.get("model_version") != MODEL_VERSION
            or row.get("policy_sha256") != policy_sha256
            or str(row.get("evidence_semantics_version") or "")
                not in {"", EVIDENCE_SEMANTICS_VERSION}
        ):
            continue
        forecast_id = str(row.get("forecast_id") or "")
        if not forecast_id:
            continue
        if row.get("event_type") == "FORECAST":
            forecasts.setdefault(forecast_id, row)
        elif row.get("event_type") == "FORECAST_FINAL":
            finals.setdefault(forecast_id, row)

    output: list[dict[str, Any]] = []
    for forecast_id, final in finals.items():
        origin = forecasts.get(forecast_id)
        if origin is None:
            continue
        probability = finite(final.get("external_only_yes"), finite(final.get("model_yes")))
        actual = finite(final.get("actual_yes"))
        market_id = str(final.get("market_id") or origin.get("market_id") or "")
        rules_hash = str(origin.get("rules_hash") or "")
        timestamp_ms = int(finite(final.get("timestamp_ms"), 0.0))
        if (
            not market_id or len(rules_hash) != 64 or timestamp_ms <= 0
            or not 0.0 < probability < 1.0 or actual not in {0.0, 1.0}
        ):
            continue
        output.append({
            "forecast_id": forecast_id,
            "market_id": market_id,
            "rules_hash": rules_hash,
            "probability": probability,
            "actual": actual,
            "timestamp_ms": timestamp_ms,
        })
    output.sort(key=lambda row: (
        int(row["timestamp_ms"]), str(row["market_id"]), str(row["forecast_id"])
    ))
    return output


def fit_cluster_equal_platt(
    rows: list[dict[str, Any]], *, ridge: float = 1e-3, max_iter: int = 80,
) -> tuple[float, float]:
    counts = Counter(str(row["market_id"]) for row in rows)
    intercept, slope = 0.0, 1.0
    for _ in range(max_iter):
        g0, g1 = ridge * intercept, ridge * (slope - 1.0)
        h00, h01, h11 = ridge, 0.0, ridge
        for row in rows:
            probability = min(1.0 - 1e-9, max(1e-9, float(row["probability"])))
            x = math.log(probability / (1.0 - probability))
            linear = intercept + slope * x
            fitted = 1.0 / (1.0 + math.exp(-linear)) if linear >= 0.0 \
                else math.exp(linear) / (1.0 + math.exp(linear))
            weight = 1.0 / counts[str(row["market_id"])]
            error = (fitted - float(row["actual"])) * weight
            variance = max(1e-9, fitted * (1.0 - fitted)) * weight
            g0 += error
            g1 += error * x
            h00 += variance
            h01 += variance * x
            h11 += variance * x * x
        determinant = h00 * h11 - h01 * h01
        if abs(determinant) < 1e-12:
            break
        delta_intercept = (h11 * g0 - h01 * g1) / determinant
        delta_slope = (-h01 * g0 + h00 * g1) / determinant
        intercept -= delta_intercept
        slope = min(5.0, max(0.05, slope - delta_slope))
        if abs(delta_intercept) + abs(delta_slope) < 1e-9:
            break
    return intercept, slope


def calibrated(probability: float, intercept: float, slope: float) -> float:
    probability = min(1.0 - 1e-9, max(1e-9, probability))
    value = intercept + slope * math.log(probability / (1.0 - probability))
    return 1.0 / (1.0 + math.exp(-value)) if value >= 0.0 \
        else math.exp(value) / (1.0 + math.exp(value))


def atomic_json(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name + f".tmp.{os.getpid()}")
    temporary.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    os.replace(temporary, path)


def freeze_challenger(
    *, tape_paths: list[Path], registry_root: Path, config_path: Path,
    model_sha: str, status_path: Path, minimum_contracts: int = 20,
) -> dict[str, Any]:
    policy_sha256 = canonical_policy_hash(config_path)
    values = records(tape_paths).values()
    rows = settled_rows(values, policy_sha256=policy_sha256)
    contracts = {str(row["market_id"]) for row in rows}
    outcomes = {int(row["actual"]) for row in rows}
    status: dict[str, Any] = {
        "schema": "polymarket_v7_external_fair_challenger_status_v1",
        "paper_only": True,
        "authenticated_execution": False,
        "real_order_submission": False,
        "execution_authority": "SHADOW_ZERO_AUTHORITY",
        "model_sha": model_sha,
        "settled_forecasts": len(rows),
        "independent_settlement_markets": len(contracts),
        "minimum_independent_settlement_markets": minimum_contracts,
    }
    registry = FairValueRegistry(registry_root)
    pointer = registry.challenger_pointer
    if pointer.exists():
        try:
            prior = json.loads(pointer.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            prior = {}
        if prior.get("role") == "CHALLENGER":
            artifact_path = Path(str(prior.get("artifact") or ""))
            if not artifact_path.is_absolute():
                artifact_path = Path.cwd() / artifact_path
            try:
                artifact = json.loads(artifact_path.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError):
                artifact = {}
            if artifact.get("code_sha") == model_sha:
                status.update({
                    "state": "FROZEN_CHALLENGER_REUSED",
                    "model_hash": prior.get("model_hash"),
                    "model_version": prior.get("model_version"),
                    "pointer": str(pointer),
                })
                atomic_json(status_path, status)
                return status

    if len(contracts) < minimum_contracts or outcomes != {0, 1}:
        status["state"] = "INSUFFICIENT_INDEPENDENT_SETTLEMENT_EVIDENCE"
        atomic_json(status_path, status)
        return status

    intercept, slope = fit_cluster_equal_platt(rows)
    raw_brier = sum((float(row["probability"]) - float(row["actual"])) ** 2
                    for row in rows) / len(rows)
    fitted_brier = sum((calibrated(float(row["probability"]), intercept, slope)
                        - float(row["actual"])) ** 2 for row in rows) / len(rows)
    start_ns = min(int(row["timestamp_ms"]) for row in rows) * 1_000_000
    end_ns = max(int(row["timestamp_ms"]) for row in rows) * 1_000_000
    rules_hashes = tuple(sorted({str(row["rules_hash"]) for row in rows}))
    artifact = FairModelArtifact.build(
        family="external_settlement_fair_platt",
        model_version=f"external-platt-shadow-{end_ns}",
        feature_schema_version="settlement-structural-v1",
        code_sha=model_sha,
        policy_version=policy_sha256,
        artifact_role="CHALLENGER",
        training_start_ns=start_ns,
        training_end_ns=end_ns,
        training_contracts=len(contracts),
        training_days=max(1, len({int(row["timestamp_ms"]) // 86_400_000 for row in rows})),
        assets=("BTC",),
        contract_templates=("BTC_USD_UPDOWN_5M",),
        rules_hashes=rules_hashes,
        parameters={
            "calibration_intercept": intercept,
            "calibration_slope": slope,
        },
        hyperparameters={
            "ridge": 1e-3,
            "cluster_weighting": "equal_weight_settlement_market",
            "random_shuffle": False,
            "forward_oos_starts_after_ns": end_ns,
        },
        oos_scores={
            "state": "AWAITING_IMMUTABLE_FORWARD_SETTLEMENTS",
            "training_raw_brier_diagnostic": raw_brier,
            "training_fitted_brier_diagnostic": fitted_brier,
        },
        probability_interval_diagnostics={
            "state": "AWAITING_IMMUTABLE_FORWARD_SETTLEMENTS",
        },
        economic_replay={
            "state": "AWAITING_IMMUTABLE_FORWARD_SETTLEMENTS",
            "execution_authority": "SHADOW_ZERO_AUTHORITY",
        },
    )
    registry.publish_challenger(artifact)
    status.update({
        "state": "FROZEN_CHALLENGER_PUBLISHED",
        "model_hash": artifact.model_hash,
        "model_version": artifact.model_version,
        "training_end_ns": end_ns,
        "calibration_intercept": intercept,
        "calibration_slope": slope,
        "training_raw_brier_diagnostic": raw_brier,
        "training_fitted_brier_diagnostic": fitted_brier,
        "pointer": str(pointer),
    })
    atomic_json(status_path, status)
    return status


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--tape", action="append", type=Path, default=[])
    parser.add_argument("--registry", type=Path, required=True)
    parser.add_argument("--config", type=Path, required=True)
    parser.add_argument("--model-sha", required=True)
    parser.add_argument("--status", type=Path, required=True)
    parser.add_argument("--minimum-contracts", type=int, default=20)
    args = parser.parse_args()
    if len(args.model_sha) != 40 or any(ch not in "0123456789abcdef" for ch in args.model_sha):
        raise SystemExit("exact 40-hex model SHA required")
    result = freeze_challenger(
        tape_paths=args.tape,
        registry_root=args.registry,
        config_path=args.config,
        model_sha=args.model_sha,
        status_path=args.status,
        minimum_contracts=max(2, args.minimum_contracts),
    )
    print(json.dumps(result, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
