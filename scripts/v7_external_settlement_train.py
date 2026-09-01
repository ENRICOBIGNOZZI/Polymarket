#!/usr/bin/env python3
"""Train an interpretable BTC 5m settlement-margin challenger artifact.

Splits are chronological by whole market, row weights are equalized by market,
and publication is refused from a dirty worktree.  Training never promotes a
model and grants no execution authority.
"""
from __future__ import annotations

import argparse
from collections import Counter, defaultdict
from dataclasses import asdict
import json
import math
import os
from pathlib import Path
import statistics
import subprocess
import time
from typing import Any, Iterable

try:
    from v7_external_economic_common import atomic_json, canonical_sha256, finite
    from v7_external_settlement_dataset import FEATURE_SCHEMA, MODEL_FEATURE_NAMES
    from v7_external_settlement_model import FAMILY
    from v7_fair_value_registry import FairModelArtifact, FairValueRegistry
except ModuleNotFoundError:
    from scripts.v7_external_economic_common import atomic_json, canonical_sha256, finite
    from scripts.v7_external_settlement_dataset import FEATURE_SCHEMA, MODEL_FEATURE_NAMES
    from scripts.v7_external_settlement_model import FAMILY
    from scripts.v7_fair_value_registry import FairModelArtifact, FairValueRegistry


SCHEMA = "polymarket_v7_external_settlement_training_v1"


def read_dataset(path: Path) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    with path.open(encoding="utf-8") as handle:
        for line_number, line in enumerate(handle, 1):
            if not line.strip():
                continue
            row = json.loads(line)
            if not isinstance(row, dict):
                raise ValueError(f"dataset:{line_number}:not_object")
            claimed = str(row.get("row_sha256") or "")
            unsigned = dict(row)
            unsigned.pop("row_sha256", None)
            if claimed != canonical_sha256(unsigned):
                raise ValueError(f"dataset:{line_number}:row_hash_mismatch")
            if row.get("feature_schema") != FEATURE_SCHEMA or row.get("causality_valid") is not True:
                raise ValueError(f"dataset:{line_number}:schema_or_causality")
            features = row.get("features") if isinstance(row.get("features"), dict) else {}
            if any(finite(features.get(name)) is None for name in MODEL_FEATURE_NAMES):
                raise ValueError(f"dataset:{line_number}:feature_missing")
            if finite(row.get("target_settlement_margin_bps")) is None:
                raise ValueError(f"dataset:{line_number}:target_missing")
            rows.append(row)
    return sorted(rows, key=lambda row: (int(row["observed_ms"]), str(row["market_id"])))


def chronological_market_split(rows: list[dict[str, Any]]) -> dict[str, list[dict[str, Any]]]:
    first: dict[str, int] = {}
    for row in rows:
        market = str(row["market_id"])
        first[market] = min(first.get(market, int(row["observed_ms"])), int(row["observed_ms"]))
    markets = sorted(first, key=lambda market: (first[market], market))
    n = len(markets)
    train_end = max(1, int(math.floor(0.60 * n)))
    validation_end = max(train_end + 1, int(math.floor(0.80 * n))) if n >= 3 else train_end
    validation_end = min(n, validation_end)
    assignments = {
        market: "train" if index < train_end else "validation" if index < validation_end else "test"
        for index, market in enumerate(markets)
    }
    return {
        name: [row for row in rows if assignments[str(row["market_id"])] == name]
        for name in ("train", "validation", "test")
    }


def _solve(matrix: list[list[float]], vector: list[float]) -> list[float]:
    n = len(vector)
    augmented = [list(matrix[index]) + [vector[index]] for index in range(n)]
    for column in range(n):
        pivot = max(range(column, n), key=lambda row: abs(augmented[row][column]))
        if abs(augmented[pivot][column]) < 1e-12:
            raise ValueError("singular_design_matrix")
        augmented[column], augmented[pivot] = augmented[pivot], augmented[column]
        divisor = augmented[column][column]
        augmented[column] = [value / divisor for value in augmented[column]]
        for row in range(n):
            if row == column:
                continue
            factor = augmented[row][column]
            augmented[row] = [
                value - factor * pivot_value
                for value, pivot_value in zip(augmented[row], augmented[column])
            ]
    return [augmented[index][-1] for index in range(n)]


def _cluster_weights(rows: list[dict[str, Any]]) -> list[float]:
    counts = Counter(str(row["market_id"]) for row in rows)
    return [1.0 / counts[str(row["market_id"])] for row in rows]


def fit_ridge(rows: list[dict[str, Any]], ridge: float = 1.0) -> dict[str, Any]:
    if not rows:
        raise ValueError("empty_training_rows")
    weights = _cluster_weights(rows)
    total_weight = sum(weights)
    means: dict[str, float] = {}
    scales: dict[str, float] = {}
    for name in MODEL_FEATURE_NAMES:
        values = [float(row["features"][name]) for row in rows]
        mean = sum(weight * value for weight, value in zip(weights, values)) / total_weight
        variance = sum(weight * (value - mean) ** 2 for weight, value in zip(weights, values)) / total_weight
        means[name] = mean
        scales[name] = max(math.sqrt(variance), 1e-9)
    design = [[1.0] + [
        (float(row["features"][name]) - means[name]) / scales[name]
        for name in MODEL_FEATURE_NAMES
    ] for row in rows]
    targets = [float(row["target_settlement_margin_bps"]) for row in rows]
    width = len(MODEL_FEATURE_NAMES) + 1
    gram = [[0.0] * width for _ in range(width)]
    rhs = [0.0] * width
    for weight, vector, target in zip(weights, design, targets):
        for left in range(width):
            rhs[left] += weight * vector[left] * target
            for right in range(width):
                gram[left][right] += weight * vector[left] * vector[right]
    for index in range(1, width):
        gram[index][index] += max(0.0, ridge)
    coefficients = _solve(gram, rhs)
    return {
        "intercept": coefficients[0],
        "coefficients": dict(zip(MODEL_FEATURE_NAMES, coefficients[1:])),
        "feature_means": means,
        "feature_scales": scales,
        "feature_names": list(MODEL_FEATURE_NAMES),
    }


def predict_margin(model: dict[str, Any], row: dict[str, Any]) -> float:
    value = float(model["intercept"])
    for name in model["feature_names"]:
        standardized = (
            float(row["features"][name]) - float(model["feature_means"][name])
        ) / float(model["feature_scales"][name])
        value += float(model["coefficients"][name]) * standardized
    return value


def _weighted_sigma(rows: list[dict[str, Any]], model: dict[str, Any]) -> float:
    if not rows:
        return math.nan
    weights = _cluster_weights(rows)
    errors = [float(row["target_settlement_margin_bps"]) - predict_margin(model, row) for row in rows]
    return math.sqrt(sum(weight * error * error for weight, error in zip(weights, errors)) / sum(weights))


def residual_sigma_buckets(rows: list[dict[str, Any]], model: dict[str, Any]) -> tuple[float, list[dict[str, Any]]]:
    default = max(1e-6, _weighted_sigma(rows, model))
    output: list[dict[str, Any]] = []
    for minimum, maximum in ((0.0, 15.0), (15.0, 60.0), (60.0, 180.0), (180.0, 300.0)):
        selected = [
            row for row in rows
            if minimum <= float(row["features"]["tte_seconds"]) <= maximum
        ]
        sigma = _weighted_sigma(selected, model)
        output.append({
            "minimum_seconds": minimum, "maximum_seconds": maximum,
            "sigma_bps": max(1e-6, sigma) if math.isfinite(sigma) else default,
            "rows": len(selected),
            "contracts": len({row["market_id"] for row in selected}),
        })
    return default, output


def _normal_probability(margin: float, sigma: float) -> float:
    return min(1.0 - 1e-9, max(1e-9, 0.5 * math.erfc(-margin / sigma / math.sqrt(2.0))))


def fit_platt(rows: list[dict[str, Any]], probabilities: list[float], ridge: float = 1e-3) -> tuple[float, float]:
    if not rows or len({int(row["actual_yes"]) for row in rows}) < 2:
        return 0.0, 1.0
    counts = Counter(str(row["market_id"]) for row in rows)
    intercept, slope = 0.0, 1.0
    for _ in range(80):
        g0, g1 = ridge * intercept, ridge * (slope - 1.0)
        h00, h01, h11 = ridge, 0.0, ridge
        for row, probability in zip(rows, probabilities):
            x = math.log(probability / (1.0 - probability))
            linear = intercept + slope * x
            fitted = 1.0 / (1.0 + math.exp(-linear)) if linear >= 0.0 else math.exp(linear) / (1.0 + math.exp(linear))
            weight = 1.0 / counts[str(row["market_id"])]
            error = (fitted - float(row["actual_yes"])) * weight
            variance = max(1e-9, fitted * (1.0 - fitted)) * weight
            g0 += error; g1 += error * x
            h00 += variance; h01 += variance * x; h11 += variance * x * x
        determinant = h00 * h11 - h01 * h01
        if abs(determinant) < 1e-12:
            break
        delta0 = (h11 * g0 - h01 * g1) / determinant
        delta1 = (-h01 * g0 + h00 * g1) / determinant
        intercept -= delta0
        slope = min(5.0, max(0.05, slope - delta1))
        if abs(delta0) + abs(delta1) < 1e-9:
            break
    return intercept, slope


def calibrated(probability: float, intercept: float, slope: float) -> float:
    value = intercept + slope * math.log(probability / (1.0 - probability))
    return 1.0 / (1.0 + math.exp(-value)) if value >= 0.0 else math.exp(value) / (1.0 + math.exp(value))


def scores(rows: list[dict[str, Any]], probabilities: list[float], margins: list[float]) -> dict[str, Any]:
    if not rows:
        return {"rows": 0, "contracts": 0, "brier": None, "log_loss": None, "margin_rmse_bps": None}
    actuals = [float(row["actual_yes"]) for row in rows]
    targets = [float(row["target_settlement_margin_bps"]) for row in rows]
    clipped = [min(1.0 - 1e-12, max(1e-12, value)) for value in probabilities]
    return {
        "rows": len(rows), "contracts": len({row["market_id"] for row in rows}),
        "brier": statistics.fmean((probability - actual) ** 2 for probability, actual in zip(clipped, actuals)),
        "log_loss": statistics.fmean(
            -(actual * math.log(probability) + (1.0 - actual) * math.log(1.0 - probability))
            for probability, actual in zip(clipped, actuals)
        ),
        "margin_rmse_bps": math.sqrt(statistics.fmean(
            (prediction - target) ** 2 for prediction, target in zip(margins, targets)
        )),
    }


def train_artifact(
    rows: list[dict[str, Any]], *, code_sha: str, policy_version: str,
    dataset_sha256: str, ridge: float = 1.0, minimum_contracts: int = 30,
    artifact_role: str = "RESEARCH",
) -> tuple[FairModelArtifact, dict[str, Any]]:
    contracts = {str(row["market_id"]) for row in rows}
    if len(contracts) < minimum_contracts:
        raise ValueError("insufficient_independent_contracts")
    split = chronological_market_split(rows)
    if not split["train"] or not split["validation"] or not split["test"]:
        raise ValueError("insufficient_chronological_splits")
    model = fit_ridge(split["train"], ridge)
    default_sigma, sigma_buckets = residual_sigma_buckets(split["train"], model)
    validation_margins = [predict_margin(model, row) for row in split["validation"]]
    validation_raw = [_normal_probability(value, default_sigma) for value in validation_margins]
    calibration_intercept, calibration_slope = fit_platt(split["validation"], validation_raw)
    model.update({
        "default_residual_sigma_bps": default_sigma,
        "residual_sigma_by_tte": sigma_buckets,
        "mean_uncertainty_bps": max(default_sigma * 0.25, default_sigma / math.sqrt(max(1, len({row['observed_day'] for row in split['train']})))),
        "calibration": {"intercept": calibration_intercept, "slope": calibration_slope},
        "target": "terminal_chainlink_twap_margin_bps_vs_contract_reference",
        "settlement_window_decomposition": "UNAVAILABLE_RAW_TWAP_CONSTITUENTS",
    })
    split_scores: dict[str, Any] = {}
    for name, values in split.items():
        margins = [predict_margin(model, row) for row in values]
        probabilities = [
            calibrated(_normal_probability(margin, default_sigma), calibration_intercept, calibration_slope)
            for margin in margins
        ]
        split_scores[name] = scores(values, probabilities, margins)
    train_rows = split["train"]
    fitting_rows = [*split["train"], *split["validation"]]
    training_start = min(int(row["observed_ms"]) for row in fitting_rows) * 1_000_000
    training_end = max(int(row["observed_ms"]) for row in fitting_rows) * 1_000_000
    forward_oos_start = max(int(row["observed_ms"]) for row in rows) * 1_000_000
    rules_hashes = tuple(sorted({str(row["rules_hash"]) for row in fitting_rows}))
    artifact = FairModelArtifact.build(
        family=FAMILY,
        model_version=f"btc5m-settlement-linear-{dataset_sha256[:16]}-{training_end}",
        feature_schema_version=FEATURE_SCHEMA,
        code_sha=code_sha,
        policy_version=policy_version,
        artifact_role=artifact_role,
        training_start_ns=training_start,
        training_end_ns=training_end,
        training_contracts=len({row["market_id"] for row in fitting_rows}),
        training_days=len({row["observed_day"] for row in fitting_rows}),
        assets=("BTC",),
        contract_templates=("BTC_USD_UPDOWN_5M",),
        rules_hashes=rules_hashes,
        parameters=model,
        hyperparameters={
            "ridge": ridge, "cluster_weighting": "equal_weight_settlement_market",
            "split": "chronological_whole_market_60_20_20",
            "random_shuffle": False, "dataset_sha256": dataset_sha256,
            "market_price_features_used": False,
            "linear_fit_contracts": len({row["market_id"] for row in train_rows}),
            "calibration_contracts": len({row["market_id"] for row in split["validation"]}),
            "forward_oos_starts_after_ns": forward_oos_start,
        },
        oos_scores={
            "validation": split_scores["validation"],
            "test": split_scores["test"],
            "test_not_used_for_model_or_calibration_selection": True,
        },
        probability_interval_diagnostics={
            "state": "INITIAL_RESIDUAL_AND_MEAN_UNCERTAINTY_REQUIRES_FORWARD_VALIDATION",
            "mean_uncertainty_bps": model["mean_uncertainty_bps"],
        },
        economic_replay={
            "state": "AWAITING_IMMUTABLE_FORWARD_POLICY_REPLAY",
            "execution_authority": "SHADOW_ZERO_AUTHORITY",
        },
    )
    report = {
        "schema": SCHEMA, "dataset_sha256": dataset_sha256,
        "independent_contracts": len(contracts),
        "splits": split_scores,
        "split_contracts": {
            name: len({row["market_id"] for row in values}) for name, values in split.items()
        },
        "model_hash": artifact.model_hash, "model_version": artifact.model_version,
        "automatic_promotion": False, "execution_authority": "SHADOW_ZERO_AUTHORITY",
    }
    return artifact, report


def _git(repo: Path, *args: str) -> str:
    return subprocess.check_output(["git", "-C", str(repo), *args], text=True).strip()


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--dataset", type=Path, required=True)
    parser.add_argument("--manifest", type=Path, required=True)
    parser.add_argument("--config", type=Path, required=True)
    parser.add_argument("--repo", type=Path, default=Path("."))
    parser.add_argument("--artifact", type=Path, required=True)
    parser.add_argument("--report", type=Path, required=True)
    parser.add_argument("--registry", type=Path)
    parser.add_argument("--publish-challenger", action="store_true")
    parser.add_argument("--minimum-contracts", type=int, default=30)
    parser.add_argument("--ridge", type=float, default=1.0)
    args = parser.parse_args()
    repo = args.repo.resolve()
    head = _git(repo, "rev-parse", "HEAD")
    dirty = bool(_git(repo, "status", "--porcelain=v1", "--untracked-files=all"))
    if args.publish_challenger and dirty:
        raise SystemExit("dirty worktree cannot publish an exact-SHA challenger")
    manifest = json.loads(args.manifest.read_text(encoding="utf-8"))
    config = json.loads(args.config.read_text(encoding="utf-8"))
    policy_version = canonical_sha256(config)
    artifact, report = train_artifact(
        read_dataset(args.dataset), code_sha=head, policy_version=policy_version,
        dataset_sha256=str(manifest["dataset_sha256"]), ridge=max(0.0, args.ridge),
        minimum_contracts=max(3, args.minimum_contracts),
        artifact_role="CHALLENGER" if args.publish_challenger else "RESEARCH",
    )
    atomic_json(args.artifact, asdict(artifact))
    report.update({
        "generated_at_unix_ms": int(time.time() * 1000),
        "repository_head": head, "repository_dirty": dirty,
        "published_challenger": False,
    })
    if args.publish_challenger:
        if args.registry is None:
            raise SystemExit("--registry is required with --publish-challenger")
        pointer = FairValueRegistry(args.registry).publish_challenger(artifact)
        report.update({"published_challenger": True, "pointer": str(pointer)})
    atomic_json(args.report, report)
    print(json.dumps(report, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
