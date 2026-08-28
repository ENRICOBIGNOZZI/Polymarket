#!/usr/bin/env python3
"""Chronological contract-level research for V7 settlement fair value.

The statistical unit is the contract/TTE observation, not the market-data tick.
Random shuffling is intentionally absent. Model selection and promotion remain
separate; this module only trains/evaluates challenger artifacts.
"""
from __future__ import annotations

import argparse
import json
import math
import statistics
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any, Iterable, Sequence

SCHEMA_VERSION = 1
EPS = 1e-9
EDGE_BINS = (0.0, 0.001, 0.0025, 0.005, 0.01, 0.02, 0.05, math.inf)


def clamp_probability(value: float) -> float:
    return min(1.0 - EPS, max(EPS, float(value)))


def logit(value: float) -> float:
    p = clamp_probability(value)
    return math.log(p / (1.0 - p))


def logistic(value: float) -> float:
    if value >= 0.0:
        e = math.exp(-value)
        return 1.0 / (1.0 + e)
    e = math.exp(value)
    return e / (1.0 + e)


@dataclass(frozen=True)
class FairObservation:
    contract_id: str
    market_handle: int
    rules_hash: str
    contract_version: int
    reference_version: int
    timestamp_ns: int
    day: str
    tte_seconds: float
    pm_mid: float
    oracle_only_probability: float
    external_median_probability: float
    structural_probability: float
    full_external_probability: float
    lower_probability: float
    upper_probability: float
    outcome: int
    causal_cut_id: int
    max_input_receive_ns: int
    model_version: str

    def validate(self) -> None:
        if not self.contract_id:
            raise ValueError("contract_id:missing")
        if self.outcome not in (0, 1):
            raise ValueError("outcome:not_binary")
        if self.timestamp_ns <= 0 or self.max_input_receive_ns <= 0:
            raise ValueError("clock:missing")
        if self.max_input_receive_ns > self.timestamp_ns:
            raise ValueError("causality:future_receive")
        if self.causal_cut_id <= 0:
            raise ValueError("causal_cut:missing")
        for name in (
            "pm_mid",
            "oracle_only_probability",
            "external_median_probability",
            "structural_probability",
            "full_external_probability",
            "lower_probability",
            "upper_probability",
        ):
            value = float(getattr(self, name))
            if not math.isfinite(value) or not 0.0 <= value <= 1.0:
                raise ValueError(f"{name}:out_of_range")
        if not self.lower_probability <= self.full_external_probability <= self.upper_probability:
            raise ValueError("interval:not_ordered")
        if not math.isfinite(self.tte_seconds) or self.tte_seconds < 0.0:
            raise ValueError("tte:invalid")


@dataclass(frozen=True)
class PlattModel:
    intercept: float
    slope: float
    training_contracts: int
    training_observations: int

    def predict(self, probability: float) -> float:
        return clamp_probability(logistic(self.intercept + self.slope * logit(probability)))


@dataclass(frozen=True)
class ScoreSummary:
    observations: int
    contracts: int
    days: int
    log_loss: float
    brier: float
    ece: float
    calibration_intercept: float
    calibration_slope: float


def load_observations(path: Path) -> list[FairObservation]:
    rows: list[FairObservation] = []
    with Path(path).open(encoding="utf-8") as handle:
        for line_no, line in enumerate(handle, 1):
            if not line.strip():
                continue
            raw = json.loads(line)
            try:
                row = FairObservation(**raw)
                row.validate()
            except Exception as exc:
                raise ValueError(f"{path}:{line_no}:{exc}") from exc
            rows.append(row)
    rows.sort(key=lambda row: (row.timestamp_ns, row.contract_id, row.tte_seconds))
    return rows


def _contract_weights(rows: Sequence[FairObservation]) -> list[float]:
    counts: dict[str, int] = {}
    for row in rows:
        counts[row.contract_id] = counts.get(row.contract_id, 0) + 1
    return [1.0 / counts[row.contract_id] for row in rows]


def fit_platt(rows: Sequence[FairObservation], *, source: str = "full_external_probability",
              max_iter: int = 50, ridge: float = 1e-4) -> PlattModel:
    if not rows:
        raise ValueError("fit:no_rows")
    contracts = {row.contract_id for row in rows}
    if len(contracts) < 2:
        raise ValueError("fit:insufficient_independent_contracts")
    weights = _contract_weights(rows)
    a = 0.0
    b = 1.0
    for _ in range(max_iter):
        g0 = ridge * a
        g1 = ridge * (b - 1.0)
        h00 = ridge
        h01 = 0.0
        h11 = ridge
        for row, weight in zip(rows, weights):
            x = logit(float(getattr(row, source)))
            p = logistic(a + b * x)
            error = (p - row.outcome) * weight
            variance = max(EPS, p * (1.0 - p)) * weight
            g0 += error
            g1 += error * x
            h00 += variance
            h01 += variance * x
            h11 += variance * x * x
        det = h00 * h11 - h01 * h01
        if abs(det) < 1e-12:
            break
        da = (h11 * g0 - h01 * g1) / det
        db = (-h01 * g0 + h00 * g1) / det
        a -= da
        b -= db
        b = max(0.05, min(5.0, b))
        if abs(da) + abs(db) < 1e-9:
            break
    return PlattModel(a, b, len(contracts), len(rows))


def _fit_calibration_line(probabilities: Sequence[float], outcomes: Sequence[int]) -> tuple[float, float]:
    # Reuse the same stable weighted logistic fit with synthetic one-observation
    # contracts; this metric is descriptive and never promoted as a model.
    if len(probabilities) < 2 or len(set(outcomes)) < 2:
        return math.nan, math.nan
    rows = [
        FairObservation(
            contract_id=f"metric-{i}", market_handle=i + 1, rules_hash="metric",
            contract_version=1, reference_version=1, timestamp_ns=i + 1,
            day="metric", tte_seconds=0.0, pm_mid=p,
            oracle_only_probability=p, external_median_probability=p,
            structural_probability=p, full_external_probability=p,
            lower_probability=max(0.0, p - 0.01), upper_probability=min(1.0, p + 0.01),
            outcome=int(y), causal_cut_id=i + 1, max_input_receive_ns=i + 1,
            model_version="metric",
        )
        for i, (p, y) in enumerate(zip(probabilities, outcomes))
    ]
    model = fit_platt(rows, ridge=1e-6)
    return model.intercept, model.slope


def score(rows: Sequence[FairObservation], probabilities: Sequence[float], *, ece_bins: int = 10) -> ScoreSummary:
    if len(rows) != len(probabilities) or not rows:
        raise ValueError("score:shape")
    probs = [clamp_probability(p) for p in probabilities]
    outcomes = [row.outcome for row in rows]
    ll = -sum(y * math.log(p) + (1 - y) * math.log(1.0 - p) for p, y in zip(probs, outcomes)) / len(rows)
    brier = sum((p - y) ** 2 for p, y in zip(probs, outcomes)) / len(rows)
    ece = 0.0
    for bucket in range(ece_bins):
        lo = bucket / ece_bins
        hi = (bucket + 1) / ece_bins
        idx = [i for i, p in enumerate(probs) if lo <= p < hi or (bucket == ece_bins - 1 and p == 1.0)]
        if not idx:
            continue
        avg_p = sum(probs[i] for i in idx) / len(idx)
        avg_y = sum(outcomes[i] for i in idx) / len(idx)
        ece += len(idx) / len(rows) * abs(avg_p - avg_y)
    intercept, slope = _fit_calibration_line(probs, outcomes)
    return ScoreSummary(
        observations=len(rows),
        contracts=len({row.contract_id for row in rows}),
        days=len({row.day for row in rows}),
        log_loss=ll,
        brier=brier,
        ece=ece,
        calibration_intercept=intercept,
        calibration_slope=slope,
    )


def _ordered_contracts(rows: Sequence[FairObservation]) -> list[str]:
    first: dict[str, int] = {}
    for row in rows:
        first[row.contract_id] = min(first.get(row.contract_id, row.timestamp_ns), row.timestamp_ns)
    return [contract for contract, _ in sorted(first.items(), key=lambda item: (item[1], item[0]))]


def expanding_folds(rows: Sequence[FairObservation], *, min_train_contracts: int,
                    validation_contracts: int, embargo_ns: int = 0) -> list[tuple[list[FairObservation], list[FairObservation]]]:
    contracts = _ordered_contracts(rows)
    if min_train_contracts < 2 or validation_contracts < 1:
        raise ValueError("folds:invalid_sizes")
    folds: list[tuple[list[FairObservation], list[FairObservation]]] = []
    cursor = min_train_contracts
    while cursor < len(contracts):
        val_ids = set(contracts[cursor: cursor + validation_contracts])
        train_ids = set(contracts[:cursor])
        validation = [row for row in rows if row.contract_id in val_ids]
        if not validation:
            break
        validation_start = min(row.timestamp_ns for row in validation)
        train = [row for row in rows if row.contract_id in train_ids and row.timestamp_ns + embargo_ns < validation_start]
        if len({row.contract_id for row in train}) >= min_train_contracts:
            folds.append((train, validation))
        cursor += validation_contracts
    return folds


def walk_forward_predictions(rows: Sequence[FairObservation], *, source: str,
                             min_train_contracts: int,
                             validation_contracts: int,
                             embargo_ns: int = 0) -> tuple[list[FairObservation], list[float], list[dict[str, Any]]]:
    out_rows: list[FairObservation] = []
    out_probabilities: list[float] = []
    diagnostics: list[dict[str, Any]] = []
    for fold_id, (train, validation) in enumerate(expanding_folds(
        rows,
        min_train_contracts=min_train_contracts,
        validation_contracts=validation_contracts,
        embargo_ns=embargo_ns,
    ), 1):
        model = fit_platt(train, source=source)
        probabilities = [model.predict(float(getattr(row, source))) for row in validation]
        summary = score(validation, probabilities)
        diagnostics.append({
            "fold": fold_id,
            "training_contracts": model.training_contracts,
            "validation_contracts": summary.contracts,
            "calibration_intercept": model.intercept,
            "calibration_slope": model.slope,
            "scores": asdict(summary),
        })
        out_rows.extend(validation)
        out_probabilities.extend(probabilities)
    return out_rows, out_probabilities, diagnostics


def evaluate_model_ladder(rows: Sequence[FairObservation], *, min_train_contracts: int,
                          validation_contracts: int, embargo_ns: int = 0) -> dict[str, Any]:
    mapping = {
        "PM Mid": "pm_mid",
        "Oracle Only": "oracle_only_probability",
        "External Median": "external_median_probability",
        "Structural": "structural_probability",
        "Full External": "full_external_probability",
    }
    result: dict[str, Any] = {"schema_version": SCHEMA_VERSION, "models": {}}
    for name, source in mapping.items():
        if name in {"PM Mid", "Oracle Only", "External Median"}:
            summary = score(rows, [float(getattr(row, source)) for row in rows])
            result["models"][name] = {"scores": asdict(summary), "folds": []}
            continue
        oos_rows, probabilities, folds = walk_forward_predictions(
            rows,
            source=source,
            min_train_contracts=min_train_contracts,
            validation_contracts=validation_contracts,
            embargo_ns=embargo_ns,
        )
        result["models"][name] = {
            "scores": asdict(score(oos_rows, probabilities)) if oos_rows else None,
            "folds": folds,
        }
    return result


def learning_curve(rows: Sequence[FairObservation], *, source: str = "full_external_probability",
                   minimum: int = 10, step: int = 10) -> list[dict[str, Any]]:
    contracts = _ordered_contracts(rows)
    curve: list[dict[str, Any]] = []
    for n in range(minimum, len(contracts) + 1, step):
        subset_ids = set(contracts[:n])
        subset = [row for row in rows if row.contract_id in subset_ids]
        if len({row.outcome for row in subset}) < 2:
            continue
        summary = score(subset, [row.full_external_probability for row in subset])
        curve.append({"contracts": n, **asdict(summary)})
    return curve


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("dataset", type=Path)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--min-train-contracts", type=int, default=20)
    parser.add_argument("--validation-contracts", type=int, default=10)
    parser.add_argument("--embargo-seconds", type=float, default=60.0)
    args = parser.parse_args()
    rows = load_observations(args.dataset)
    if len({row.contract_id for row in rows}) < args.min_train_contracts + args.validation_contracts:
        raise SystemExit("insufficient independent contracts for requested walk-forward")
    report = evaluate_model_ladder(
        rows,
        min_train_contracts=args.min_train_contracts,
        validation_contracts=args.validation_contracts,
        embargo_ns=int(args.embargo_seconds * 1e9),
    )
    report["learning_curve"] = learning_curve(rows)
    report["dataset"] = {
        "observations": len(rows),
        "contracts": len({row.contract_id for row in rows}),
        "days": len({row.day for row in rows}),
    }
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(report, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    print(json.dumps(report["dataset"], sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
