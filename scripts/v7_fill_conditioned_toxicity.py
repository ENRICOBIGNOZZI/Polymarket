#!/usr/bin/env python3
"""Build a PAPER-only fill-conditioned maker toxicity report.

The report answers the economic question that matters after a maker fill:
    E[future executable markout | we were actually filled, causal entry state]

It consumes the canonical PAPER execution ledger plus the zero-authority C++
maker-markout evidence.  It never writes MAKE/CANCEL intents and never grants
execution authority.  The fitted ridge model is research evidence only.
"""
from __future__ import annotations

import argparse
import csv
import json
import math
from pathlib import Path
import random
import statistics
from typing import Any, Iterable

STRATEGY = "CRYPTO_SETTLEMENT_ENGINE"
COMPONENT = "professional_maker"
DEFAULT_HORIZON = "250ms"
FEATURES = (
    "microstructure_shadow_delta_250ms",
    "imbalance",
    "ofi",
    "cancel_intensity",
    "trade_intensity",
    "aggressive_sell_prints_per_second",
    "short_return_ticks",
    "local_latency_ms",
    "queue_ahead",
    "predicted_fill_probability",
)


def finite_number(value: Any) -> float | None:
    try:
        out = float(value)
    except (TypeError, ValueError):
        return None
    return out if math.isfinite(out) else None


def read_json(path: Path) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    return value if isinstance(value, dict) else {}


def iter_jsonl(path: Path) -> Iterable[dict[str, Any]]:
    if not path.exists():
        return
    with path.open(encoding="utf-8") as handle:
        for line in handle:
            try:
                value = json.loads(line)
            except json.JSONDecodeError:
                continue
            if isinstance(value, dict):
                yield value


def valid_fill(row: dict[str, Any], model_sha: str) -> bool:
    metadata = row.get("metadata")
    return bool(
        row.get("event_type") == "FILL"
        and row.get("strategy") == STRATEGY
        and row.get("model_sha") == model_sha
        and row.get("paper_only") is True
        and row.get("authenticated_execution") is False
        and isinstance(metadata, dict)
        and metadata.get("component") == COMPONENT
        and metadata.get("execution_authority") == "SIMULATED_PAPER_ONLY"
    )


def fill_record(row: dict[str, Any]) -> dict[str, Any]:
    metadata = row.get("metadata") if isinstance(row.get("metadata"), dict) else {}
    placement = metadata.get("placement_features")
    if not isinstance(placement, dict):
        placement = {}
    out: dict[str, Any] = {
        "fill_id": str(row.get("fill_id") or ""),
        "order_id": str(row.get("order_id") or ""),
        "market_id": str(row.get("market_id") or ""),
        "event_id": str(row.get("event_id") or ""),
        "token_id": str(row.get("token_id") or ""),
        "side": str(row.get("side") or ""),
        "receive_ts_ms": int(finite_number(row.get("receive_ts_ms")) or 0),
        "fill_price": finite_number(row.get("fill_price")),
        "filled_size": finite_number(row.get("filled_size")),
        "queue_ahead": finite_number(row.get("queue_ahead")),
        "predicted_fill_probability": finite_number(row.get("predicted_fill_probability")),
        "expected_ev": finite_number(row.get("expected_ev")),
        "placement_features_timestamp_ms": int(
            finite_number(metadata.get("placement_features_timestamp_ms")) or 0
        ),
        "placement_features_source": str(metadata.get("placement_features_source") or ""),
        "placement_features_snapshot_id": str(metadata.get("placement_features_snapshot_id") or ""),
    }
    for feature in FEATURES:
        if feature in {"queue_ahead", "predicted_fill_probability"}:
            continue
        out[feature] = finite_number(placement.get(feature))
    return out


def load_fills(ledger: Path, model_sha: str) -> dict[str, dict[str, Any]]:
    fills: dict[str, dict[str, Any]] = {}
    for row in iter_jsonl(ledger):
        if not valid_fill(row, model_sha):
            continue
        record = fill_record(row)
        if record["fill_id"] and record["market_id"] and record["receive_ts_ms"] > 0:
            fills[record["fill_id"]] = record
    return fills


def load_markouts(root: Path, model_sha: str) -> dict[str, dict[str, float]]:
    out: dict[str, dict[str, float]] = {}
    if not root.exists():
        return out
    for path in root.glob("*.json"):
        row = read_json(path)
        if (
            row.get("event_type") != "MARKOUT"
            or row.get("strategy") != STRATEGY
            or row.get("model_sha") != model_sha
            or row.get("paper_only") is not True
            or row.get("authenticated_execution") is not False
        ):
            continue
        metadata = row.get("metadata")
        if not isinstance(metadata, dict) or metadata.get("fill_conditioned") is not True:
            continue
        fill_id = str(row.get("fill_id") or "")
        values = row.get("markouts")
        if not fill_id or not isinstance(values, dict):
            continue
        target = out.setdefault(fill_id, {})
        for label, value in values.items():
            numeric = finite_number(value)
            if numeric is not None:
                target[str(label)] = numeric
    return out


def joined_rows(
    fills: dict[str, dict[str, Any]], markouts: dict[str, dict[str, float]]
) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for fill_id, fill in fills.items():
        row = dict(fill)
        for label, value in markouts.get(fill_id, {}).items():
            row[f"markout_{label}"] = value
        rows.append(row)
    return sorted(rows, key=lambda row: (int(row["receive_ts_ms"]), str(row["fill_id"])))


def mean(values: list[float]) -> float | None:
    return sum(values) / len(values) if values else None


def median(values: list[float]) -> float | None:
    return statistics.median(values) if values else None


def pearson(x: list[float], y: list[float]) -> float | None:
    if len(x) != len(y) or len(x) < 2:
        return None
    xm, ym = mean(x), mean(y)
    assert xm is not None and ym is not None
    dx = [value - xm for value in x]
    dy = [value - ym for value in y]
    denom = math.sqrt(sum(value * value for value in dx) * sum(value * value for value in dy))
    if denom <= 0:
        return None
    return sum(a * b for a, b in zip(dx, dy)) / denom


def percentile(values: list[float], q: float) -> float | None:
    if not values:
        return None
    ordered = sorted(values)
    if len(ordered) == 1:
        return ordered[0]
    position = min(1.0, max(0.0, q)) * (len(ordered) - 1)
    lo = int(math.floor(position))
    hi = int(math.ceil(position))
    if lo == hi:
        return ordered[lo]
    weight = position - lo
    return ordered[lo] * (1.0 - weight) + ordered[hi] * weight


def market_cluster_bootstrap(
    rows: list[dict[str, Any]], target: str, *, draws: int = 2000, seed: int = 7
) -> dict[str, Any]:
    by_market: dict[str, list[float]] = {}
    for row in rows:
        value = finite_number(row.get(target))
        market = str(row.get("market_id") or "")
        if value is not None and market:
            by_market.setdefault(market, []).append(value)
    market_means = [sum(values) / len(values) for values in by_market.values() if values]
    if not market_means:
        return {"market_count": 0, "ci95": [None, None]}
    rng = random.Random(seed)
    estimates: list[float] = []
    for _ in range(draws):
        sample = [market_means[rng.randrange(len(market_means))] for _ in market_means]
        estimates.append(sum(sample) / len(sample))
    return {
        "market_count": len(market_means),
        "market_equal_weight_mean": sum(market_means) / len(market_means),
        "ci95": [percentile(estimates, 0.025), percentile(estimates, 0.975)],
    }


def horizon_summary(rows: list[dict[str, Any]], label: str) -> dict[str, Any]:
    key = f"markout_{label}"
    values = [value for row in rows if (value := finite_number(row.get(key))) is not None]
    return {
        "horizon": label,
        "fill_count": len(values),
        "mean": mean(values),
        "median": median(values),
        "positive_fraction": (
            sum(value > 0.0 for value in values) / len(values) if values else None
        ),
        "cluster_bootstrap": market_cluster_bootstrap(rows, key),
    }


def solve_linear_system(matrix: list[list[float]], vector: list[float]) -> list[float] | None:
    n = len(vector)
    augmented = [list(matrix[i]) + [vector[i]] for i in range(n)]
    for pivot in range(n):
        best = max(range(pivot, n), key=lambda row: abs(augmented[row][pivot]))
        if abs(augmented[best][pivot]) < 1e-12:
            return None
        augmented[pivot], augmented[best] = augmented[best], augmented[pivot]
        scale = augmented[pivot][pivot]
        augmented[pivot] = [value / scale for value in augmented[pivot]]
        for row in range(n):
            if row == pivot:
                continue
            factor = augmented[row][pivot]
            if abs(factor) < 1e-18:
                continue
            augmented[row] = [
                a - factor * b for a, b in zip(augmented[row], augmented[pivot])
            ]
    return [augmented[i][-1] for i in range(n)]


def train_test_market_split(rows: list[dict[str, Any]], train_fraction: float = 0.7) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    first_seen: dict[str, int] = {}
    for row in rows:
        market = str(row.get("market_id") or "")
        if market:
            first_seen.setdefault(market, int(row.get("receive_ts_ms") or 0))
    markets = sorted(first_seen, key=lambda market: (first_seen[market], market))
    if len(markets) < 2:
        return rows, []
    cut = min(len(markets) - 1, max(1, int(len(markets) * train_fraction)))
    train_markets = set(markets[:cut])
    return (
        [row for row in rows if str(row.get("market_id") or "") in train_markets],
        [row for row in rows if str(row.get("market_id") or "") not in train_markets],
    )


def fitted_toxicity_model(
    rows: list[dict[str, Any]], horizon: str, ridge: float
) -> dict[str, Any]:
    target = f"markout_{horizon}"
    usable = [row for row in rows if finite_number(row.get(target)) is not None]
    train, test = train_test_market_split(usable)
    if len(train) < max(12, len(FEATURES) + 2) or not test:
        return {
            "status": "INSUFFICIENT_MARKET_SEPARATED_FILLS",
            "target": target,
            "train_fills": len(train),
            "test_fills": len(test),
        }

    medians: dict[str, float] = {}
    centers: dict[str, float] = {}
    scales: dict[str, float] = {}
    for feature in FEATURES:
        values = [value for row in train if (value := finite_number(row.get(feature))) is not None]
        med = statistics.median(values) if values else 0.0
        medians[feature] = med
        completed = [finite_number(row.get(feature)) for row in train]
        completed_values = [value if value is not None else med for value in completed]
        center = mean(completed_values) or 0.0
        variance = mean([(value - center) ** 2 for value in completed_values]) or 0.0
        centers[feature] = center
        scales[feature] = math.sqrt(variance) if variance > 1e-12 else 1.0

    def design(row: dict[str, Any]) -> list[float]:
        values = [1.0]
        for feature in FEATURES:
            value = finite_number(row.get(feature))
            if value is None:
                value = medians[feature]
            values.append((value - centers[feature]) / scales[feature])
        return values

    p = len(FEATURES) + 1
    xtx = [[0.0 for _ in range(p)] for _ in range(p)]
    xty = [0.0 for _ in range(p)]
    for row in train:
        x = design(row)
        y = float(row[target])
        for i in range(p):
            xty[i] += x[i] * y
            for j in range(p):
                xtx[i][j] += x[i] * x[j]
    for i in range(1, p):
        xtx[i][i] += max(0.0, ridge)
    coefficients = solve_linear_system(xtx, xty)
    if coefficients is None:
        return {"status": "SINGULAR_DESIGN", "target": target}

    predictions: list[float] = []
    outcomes: list[float] = []
    scored: list[dict[str, Any]] = []
    for row in test:
        x = design(row)
        prediction = sum(a * b for a, b in zip(coefficients, x))
        outcome = float(row[target])
        predictions.append(prediction)
        outcomes.append(outcome)
        scored.append({**row, "toxicity_prediction": prediction})
    errors = [prediction - outcome for prediction, outcome in zip(predictions, outcomes)]
    threshold = percentile(predictions, 0.75)
    selected = [row for row in scored if threshold is not None and row["toxicity_prediction"] >= threshold]
    selected_bootstrap = market_cluster_bootstrap(selected, target)
    selected_ci = selected_bootstrap.get("ci95") or [None, None]
    lower = selected_ci[0] if selected_ci else None
    research_gate = bool(lower is not None and lower > 0.0 and len(selected) >= 20)

    return {
        "status": "OK",
        "target": target,
        "ridge": ridge,
        "features": list(FEATURES),
        "train_fills": len(train),
        "test_fills": len(test),
        "train_markets": len({str(row.get('market_id') or '') for row in train}),
        "test_markets": len({str(row.get('market_id') or '') for row in test}),
        "coefficients_standardized": {
            "intercept": coefficients[0],
            **{feature: coefficients[index + 1] for index, feature in enumerate(FEATURES)},
        },
        "test_rmse": math.sqrt(mean([error * error for error in errors]) or 0.0),
        "test_mae": mean([abs(error) for error in errors]),
        "test_prediction_outcome_correlation": pearson(predictions, outcomes),
        "test_mean_markout": mean(outcomes),
        "top_prediction_quartile_threshold": threshold,
        "top_prediction_quartile_fill_count": len(selected),
        "top_prediction_quartile_mean_markout": mean(
            [float(row[target]) for row in selected]
        ),
        "top_prediction_quartile_cluster_bootstrap": selected_bootstrap,
        "research_gate_lower_ci_positive": research_gate,
        "execution_authority": "ZERO_AUTHORITY_RESEARCH_ONLY",
    }


def write_dataset(path: Path, rows: list[dict[str, Any]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    keys = [
        "fill_id", "order_id", "market_id", "event_id", "token_id", "side",
        "receive_ts_ms", "fill_price", "filled_size", "queue_ahead",
        "predicted_fill_probability", "expected_ev", *FEATURES,
        "markout_100ms", "markout_250ms", "markout_500ms", "markout_1s",
        "markout_5s", "markout_10s", "markout_30s", "markout_45s",
        "markout_60s", "markout_300s",
    ]
    # Preserve order while removing duplicate top-level features.
    keys = list(dict.fromkeys(keys))
    with path.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=keys, extrasaction="ignore")
        writer.writeheader()
        writer.writerows(rows)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--run-root", type=Path, default=Path("runs/paper_v7_live"))
    parser.add_argument("--model-sha", required=True)
    parser.add_argument("--horizon", default=DEFAULT_HORIZON)
    parser.add_argument("--ridge", type=float, default=1.0)
    parser.add_argument("--output", type=Path)
    parser.add_argument("--dataset", type=Path)
    args = parser.parse_args()
    if len(args.model_sha) != 40 or any(ch not in "0123456789abcdef" for ch in args.model_sha):
        parser.error("--model-sha must be exact lowercase 40-hex")

    ledger = args.run_root / "ledger" / "execution.jsonl"
    evidence = args.run_root / "research" / "evidence" / "maker_markout"
    fills = load_fills(ledger, args.model_sha)
    markouts = load_markouts(evidence, args.model_sha)
    rows = joined_rows(fills, markouts)

    output = args.output or args.run_root / "reports" / "fill_conditioned_toxicity.json"
    dataset = args.dataset or args.run_root / "reports" / "fill_conditioned_toxicity.csv"
    output.parent.mkdir(parents=True, exist_ok=True)
    write_dataset(dataset, rows)

    horizon_labels = ("100ms", "250ms", "500ms", "1s", "5s", "10s", "30s", "45s", "60s", "300s")
    report = {
        "schema": "polymarket_v7_fill_conditioned_toxicity_v1",
        "paper_only": True,
        "authenticated_execution": False,
        "real_order_submission": False,
        "execution_authority": "ZERO_AUTHORITY_RESEARCH_ONLY",
        "model_sha": args.model_sha,
        "fill_count": len(rows),
        "market_count": len({str(row.get("market_id") or "") for row in rows}),
        "horizons": {label: horizon_summary(rows, label) for label in horizon_labels},
        "toxicity_model": fitted_toxicity_model(rows, args.horizon, args.ridge),
        "dataset": str(dataset),
    }
    output.write_text(json.dumps(report, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    print(json.dumps(report, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
