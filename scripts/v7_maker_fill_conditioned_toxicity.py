#!/usr/bin/env python3
"""Train and evaluate a zero-authority Maker toxicity model on canonical PAPER fills.

The feature cut is frozen at ORDER_SUBMITTED. FILL and MARKOUT records are labels only.
Splits are chronological and disjoint by event/market cluster. No output from this
script has execution authority and nothing is promoted automatically.
"""
from __future__ import annotations

import argparse
import gzip
import hashlib
import json
import math
import pathlib
import random
from collections import defaultdict
from typing import Any, Iterable

REPORT_SCHEMA = "polymarket_v7_maker_fill_conditioned_toxicity_v1"
MODEL_SCHEMA = "polymarket_v7_maker_fill_conditioned_toxicity_model_v1"
SEMANTICS = "maker-paper-v7.2-bilateral-inventory"
FEATURES = (
    "microstructure_shadow_delta_250ms",
    "imbalance",
    "ofi",
    "cancel_intensity",
    "trade_intensity",
    "short_return_ticks",
    "ew_vol_ticks",
    "aggressive_sell_prints_per_second",
    "spread_ticks",
    "distance_from_touch_ticks",
    "local_latency_ms",
    "queue_ahead",
    "fill_probability",
)


def num(value: Any, default: float = math.nan) -> float:
    try:
        out = float(value)
    except (TypeError, ValueError, OverflowError):
        return default
    return out if math.isfinite(out) else default


def stable(value: Any) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=True)


def sha256(value: Any) -> str:
    return hashlib.sha256(stable(value).encode()).hexdigest()


def record_id(row: dict[str, Any]) -> str:
    return str(row.get("record_id") or sha256(row))


def evidence_files(paths: Iterable[pathlib.Path]) -> list[pathlib.Path]:
    out: set[pathlib.Path] = set()
    for raw in paths:
        path = pathlib.Path(raw)
        if path.is_file():
            out.add(path.resolve())
            if path.name.endswith(".jsonl"):
                for segment in path.parent.glob(path.name + ".segment-*.jsonl*"):
                    if segment.is_file() and (segment.name.endswith(".jsonl") or segment.name.endswith(".jsonl.gz")):
                        out.add(segment.resolve())
            continue
        if path.exists():
            for pattern in ("*.json", "*.jsonl", "*.jsonl.gz"):
                out.update(item.resolve() for item in path.rglob(pattern) if item.is_file())
    return sorted(out)


def records(paths: Iterable[pathlib.Path]) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    seen: set[str] = set()
    for path in evidence_files(paths):
        try:
            if path.suffix == ".json":
                raw = json.loads(path.read_text(encoding="utf-8"))
                values = raw if isinstance(raw, list) else [raw]
            else:
                values = []
                opener = gzip.open if path.name.endswith(".gz") else open
                with opener(path, "rt", encoding="utf-8") as handle:
                    for line in handle:
                        try:
                            values.append(json.loads(line))
                        except json.JSONDecodeError:
                            pass
            for row in values:
                if not isinstance(row, dict):
                    continue
                identity = record_id(row)
                if identity in seen:
                    continue
                seen.add(identity)
                out.append(row)
        except (OSError, json.JSONDecodeError):
            pass
    return out


def metadata(row: dict[str, Any]) -> dict[str, Any]:
    return row.get("metadata") if isinstance(row.get("metadata"), dict) else {}


def life(row: dict[str, Any]) -> tuple[str, str]:
    return str(row.get("model_sha") or "unknown"), str(row.get("order_id") or "")


def placement(order: dict[str, Any]) -> str:
    meta = metadata(order)
    explicit = meta.get("placement_action") or order.get("placement_action")
    if explicit:
        return str(explicit).upper()
    envelope = meta.get("opportunity_envelope") if isinstance(meta.get("opportunity_envelope"), dict) else {}
    reasons = envelope.get("reasons") if isinstance(envelope.get("reasons"), list) else []
    found = {str(reason)[10:] for reason in reasons if str(reason).startswith("PLACEMENT_")}
    return found.pop().upper() if len(found) == 1 else "UNKNOWN"


def feature_cut(order: dict[str, Any]) -> dict[str, float]:
    meta = metadata(order)
    placement_features = meta.get("placement_features") if isinstance(meta.get("placement_features"), dict) else {}
    alpha = meta.get("execution_alpha") if isinstance(meta.get("execution_alpha"), dict) else {}
    alpha_features = alpha.get("features") if isinstance(alpha.get("features"), dict) else {}
    fill = alpha.get("fill_probability") if isinstance(alpha.get("fill_probability"), dict) else {}
    out: dict[str, float] = {}
    for name in FEATURES:
        if name == "queue_ahead":
            value = alpha_features.get("queue_ahead", placement_features.get("queue_ahead"))
        elif name == "fill_probability":
            value = fill.get("point")
        else:
            value = placement_features.get(name, alpha_features.get(name))
        out[name] = num(value)
    return out


def build_fill_rows(
    all_rows: list[dict[str, Any]], *, markout_horizon: str,
    placement_actions: set[str], include_bootstrap_probes: bool,
) -> tuple[list[dict[str, Any]], dict[str, int]]:
    orders: dict[tuple[str, str], dict[str, Any]] = {}
    diagnostics = {
        "candidate_orders": 0,
        "orders_missing_feature_cut": 0,
        "fills_seen": 0,
        "fills_with_markout": 0,
        "fills_missing_markout": 0,
        "fills_invalid_timing": 0,
    }
    for row in all_rows:
        if row.get("event_type") != "ORDER_SUBMITTED" or not row.get("order_id"):
            continue
        meta = metadata(row)
        semantics = str(meta.get("execution_semantics_version") or "")
        if meta.get("component") not in {None, "professional_maker"}:
            continue
        if semantics and semantics != SEMANTICS:
            continue
        if str(row.get("side") or meta.get("execution_side") or "").upper() != "BUY":
            continue
        action = placement(row)
        if action not in placement_actions:
            continue
        if not include_bootstrap_probes and meta.get("paper_bootstrap_probe") is True:
            continue
        timestamp = int(num(row.get("recorded_ts_ms"), 0))
        if timestamp <= 0:
            continue
        diagnostics["candidate_orders"] += 1
        if not any(math.isfinite(value) for value in feature_cut(row).values()):
            diagnostics["orders_missing_feature_cut"] += 1
        orders[life(row)] = row

    fills: defaultdict[tuple[str, str], list[dict[str, Any]]] = defaultdict(list)
    exact_markout: dict[tuple[str, str, str], tuple[int, float]] = {}
    fallback_markout: defaultdict[tuple[str, str], list[tuple[int, float]]] = defaultdict(list)
    for row in all_rows:
        key = life(row)
        if key not in orders:
            continue
        if row.get("event_type") == "FILL" and num(row.get("filled_size"), 0) > 0:
            fills[key].append(row)
        if row.get("event_type") != "MARKOUT" or not isinstance(row.get("markouts"), dict):
            continue
        if markout_horizon not in row["markouts"]:
            continue
        value = num(row["markouts"][markout_horizon])
        timestamp = int(num(row.get("recorded_ts_ms"), 0))
        if not math.isfinite(value) or timestamp <= 0:
            continue
        fill_id = str(row.get("fill_id") or "")
        if fill_id:
            identity = (key[0], key[1], fill_id)
            if identity not in exact_markout or timestamp < exact_markout[identity][0]:
                exact_markout[identity] = (timestamp, value)
        else:
            fallback_markout[key].append((timestamp, value))

    result: list[dict[str, Any]] = []
    for key, order_fills in fills.items():
        order = orders[key]
        meta = metadata(order)
        start_ms = int(num(order.get("recorded_ts_ms"), 0))
        action = placement(order)
        market_id = str(order.get("market_id") or "")
        cluster = str(order.get("event_id") or market_id or "UNKNOWN")
        features = feature_cut(order)
        fallback = sorted(fallback_markout[key])
        one_fill_fallback = fallback[0][1] if len(order_fills) == 1 and fallback else math.nan
        for fill_row in sorted(order_fills, key=lambda item: int(num(item.get("recorded_ts_ms"), 0))):
            diagnostics["fills_seen"] += 1
            fill_ms = int(num(fill_row.get("recorded_ts_ms"), 0))
            fill_id = str(fill_row.get("fill_id") or "")
            if fill_ms <= start_ms:
                diagnostics["fills_invalid_timing"] += 1
                continue
            exact = exact_markout.get((key[0], key[1], fill_id))
            markout = exact[1] if exact else one_fill_fallback
            if not math.isfinite(markout):
                diagnostics["fills_missing_markout"] += 1
                continue
            shares = num(fill_row.get("filled_size"), 0)
            if shares <= 0:
                continue
            diagnostics["fills_with_markout"] += 1
            result.append({
                "source_model_sha": key[0],
                "order_id": key[1],
                "fill_id": fill_id,
                "market_id": market_id,
                "event_cluster": cluster,
                "outcome": str(meta.get("outcome") or order.get("outcome") or "UNKNOWN").upper(),
                "placement_action": action,
                "order_ts_ms": start_ms,
                "fill_ts_ms": fill_ms,
                "fill_delay_ms": fill_ms - start_ms,
                "filled_shares": shares,
                "markout_per_share": markout,
                "adverse": 1 if markout < 0.0 else 0,
                "features": features,
                "feature_source": str(meta.get("placement_features_source") or ""),
                "feature_snapshot_id": str(meta.get("placement_features_snapshot_id") or ""),
            })
    result.sort(key=lambda item: (item["order_ts_ms"], item["event_cluster"], item["order_id"], item["fill_id"]))
    return result, diagnostics


def quantile(values: list[float], probability: float) -> float:
    if not values:
        return math.nan
    ordered = sorted(values)
    position = max(0.0, min(1.0, probability)) * (len(ordered) - 1)
    lo, hi = math.floor(position), math.ceil(position)
    if lo == hi:
        return ordered[lo]
    return ordered[lo] * (hi - position) + ordered[hi] * (position - lo)


def chronological_cluster_split(rows: list[dict[str, Any]]) -> tuple[list[dict[str, Any]], list[dict[str, Any]], list[dict[str, Any]], dict[str, list[str]]]:
    first: dict[str, int] = {}
    for row in rows:
        cluster = str(row["event_cluster"])
        first[cluster] = min(first.get(cluster, int(row["order_ts_ms"])), int(row["order_ts_ms"]))
    clusters = sorted(first, key=lambda cluster: (first[cluster], cluster))
    count = len(clusters)
    train_n = max(1, int(math.floor(count * 0.60))) if count else 0
    val_n = max(1, int(math.floor(count * 0.20))) if count >= 3 else 0
    if train_n + val_n >= count and count >= 3:
        train_n = max(1, count - 2)
        val_n = 1
    train_ids = set(clusters[:train_n])
    val_ids = set(clusters[train_n:train_n + val_n])
    test_ids = set(clusters[train_n + val_n:])
    return (
        [row for row in rows if row["event_cluster"] in train_ids],
        [row for row in rows if row["event_cluster"] in val_ids],
        [row for row in rows if row["event_cluster"] in test_ids],
        {"train": sorted(train_ids), "validation": sorted(val_ids), "test": sorted(test_ids)},
    )


def fit_preprocessor(rows: list[dict[str, Any]]) -> dict[str, dict[str, float]]:
    result: dict[str, dict[str, float]] = {}
    for name in FEATURES:
        values = [num(row["features"].get(name)) for row in rows]
        values = [value for value in values if math.isfinite(value)]
        if not values:
            result[name] = {"lower": 0.0, "upper": 0.0, "median": 0.0, "scale": 1.0, "observed": 0}
            continue
        lower = quantile(values, 0.02)
        upper = quantile(values, 0.98)
        median = quantile(values, 0.50)
        scale = quantile(values, 0.90) - quantile(values, 0.10)
        if not math.isfinite(scale) or abs(scale) < 1e-9:
            scale = max(abs(median), 1.0)
        result[name] = {"lower": lower, "upper": upper, "median": median, "scale": scale, "observed": len(values)}
    return result


def transform(row: dict[str, Any], prep: dict[str, dict[str, float]]) -> list[float]:
    output = [1.0]
    for name in FEATURES:
        spec = prep[name]
        value = num(row["features"].get(name), spec["median"])
        if not math.isfinite(value):
            value = spec["median"]
        value = min(spec["upper"], max(spec["lower"], value))
        output.append((value - spec["median"]) / spec["scale"])
    return output


def cluster_weights(rows: list[dict[str, Any]]) -> list[float]:
    counts: defaultdict[str, int] = defaultdict(int)
    for row in rows:
        counts[str(row["event_cluster"])] += 1
    weights = [1.0 / counts[str(row["event_cluster"])] for row in rows]
    total = sum(weights)
    return [weight * len(rows) / total for weight in weights] if total > 0 else [1.0] * len(rows)


def sigmoid(value: float) -> float:
    value = max(-35.0, min(35.0, value))
    return 1.0 / (1.0 + math.exp(-value))


def fit_logistic(rows: list[dict[str, Any]], prep: dict[str, dict[str, float]], *, ridge: float, iterations: int) -> list[float]:
    width = len(FEATURES) + 1
    beta = [0.0] * width
    weights = cluster_weights(rows)
    design = [transform(row, prep) for row in rows]
    labels = [float(row["adverse"]) for row in rows]
    denom = max(1.0, sum(weights))
    for iteration in range(max(1, iterations)):
        gradient = [0.0] * width
        for x, label, weight in zip(design, labels, weights):
            prediction = sigmoid(sum(b * value for b, value in zip(beta, x)))
            error = (prediction - label) * weight
            for index, value in enumerate(x):
                gradient[index] += error * value
        for index in range(width):
            gradient[index] /= denom
            if index:
                gradient[index] += ridge * beta[index] / max(1.0, len(rows))
        rate = 0.20 / math.sqrt(1.0 + iteration / 100.0)
        step = max(abs(value) for value in gradient)
        for index in range(width):
            beta[index] -= rate * gradient[index]
        if step < 1e-7:
            break
    return beta


def solve(matrix: list[list[float]], vector: list[float]) -> list[float]:
    size = len(vector)
    augmented = [list(matrix[row]) + [vector[row]] for row in range(size)]
    for column in range(size):
        pivot = max(range(column, size), key=lambda row: abs(augmented[row][column]))
        augmented[column], augmented[pivot] = augmented[pivot], augmented[column]
        if abs(augmented[column][column]) < 1e-12:
            augmented[column][column] = 1e-12
        scale = augmented[column][column]
        augmented[column] = [value / scale for value in augmented[column]]
        for row in range(size):
            if row == column:
                continue
            factor = augmented[row][column]
            if factor == 0.0:
                continue
            augmented[row] = [left - factor * right for left, right in zip(augmented[row], augmented[column])]
    return [augmented[row][-1] for row in range(size)]


def fit_ridge_markout(rows: list[dict[str, Any]], prep: dict[str, dict[str, float]], *, ridge: float) -> list[float]:
    width = len(FEATURES) + 1
    matrix = [[0.0] * width for _ in range(width)]
    vector = [0.0] * width
    weights = cluster_weights(rows)
    for row, weight in zip(rows, weights):
        x = transform(row, prep)
        target = float(row["markout_per_share"])
        for i in range(width):
            vector[i] += weight * x[i] * target
            for j in range(width):
                matrix[i][j] += weight * x[i] * x[j]
    for index in range(1, width):
        matrix[index][index] += ridge
    return solve(matrix, vector)


def predict_probability(row: dict[str, Any], prep: dict[str, dict[str, float]], beta: list[float]) -> float:
    x = transform(row, prep)
    return sigmoid(sum(coefficient * value for coefficient, value in zip(beta, x)))


def predict_markout(row: dict[str, Any], prep: dict[str, dict[str, float]], beta: list[float]) -> float:
    x = transform(row, prep)
    return sum(coefficient * value for coefficient, value in zip(beta, x))


def auc(labels: list[int], scores: list[float]) -> float | None:
    positives = sum(labels)
    negatives = len(labels) - positives
    if positives == 0 or negatives == 0:
        return None
    ordered = sorted(zip(scores, labels), key=lambda item: item[0])
    rank_sum = 0.0
    index = 0
    while index < len(ordered):
        end = index + 1
        while end < len(ordered) and ordered[end][0] == ordered[index][0]:
            end += 1
        average_rank = 0.5 * ((index + 1) + end)
        rank_sum += average_rank * sum(label for _, label in ordered[index:end])
        index = end
    return (rank_sum - positives * (positives + 1) / 2.0) / (positives * negatives)


def summarize(rows: list[dict[str, Any]], prep: dict[str, dict[str, float]], logistic: list[float], linear: list[float]) -> dict[str, Any]:
    if not rows:
        return {"rows": 0, "clusters": 0}
    probabilities = [predict_probability(row, prep, logistic) for row in rows]
    markout_predictions = [predict_markout(row, prep, linear) for row in rows]
    labels = [int(row["adverse"]) for row in rows]
    marks = [float(row["markout_per_share"]) for row in rows]
    shares = [float(row["filled_shares"]) for row in rows]
    brier = sum((probability - label) ** 2 for probability, label in zip(probabilities, labels)) / len(rows)
    logloss = -sum(label * math.log(max(probability, 1e-12)) + (1 - label) * math.log(max(1 - probability, 1e-12)) for probability, label in zip(probabilities, labels)) / len(rows)
    den = sum(shares)
    weighted_markout = sum(mark * share for mark, share in zip(marks, shares)) / den if den else None
    mse = sum((prediction - mark) ** 2 for prediction, mark in zip(markout_predictions, marks)) / len(rows)
    return {
        "rows": len(rows),
        "clusters": len({row["event_cluster"] for row in rows}),
        "adverse_rate": sum(labels) / len(labels),
        "auc": auc(labels, probabilities),
        "brier": brier,
        "logloss": logloss,
        "markout_mse": mse,
        "mean_markout_per_share": sum(marks) / len(marks),
        "share_weighted_markout_per_share": weighted_markout,
        "filled_shares": den,
    }


def threshold_candidates(rows: list[dict[str, Any]], prep: dict[str, dict[str, float]], logistic: list[float]) -> list[float]:
    scores = sorted({round(predict_probability(row, prep, logistic), 6) for row in rows})
    return sorted(set([0.05, 0.10, 0.15, 0.20, 0.25, 0.30, 0.40, 0.50] + scores))


def choose_safe_threshold(rows: list[dict[str, Any]], prep: dict[str, dict[str, float]], logistic: list[float], minimum_coverage: float) -> tuple[float, dict[str, Any]]:
    best: tuple[float, float, float, int] | None = None
    best_report: dict[str, Any] = {}
    for threshold in threshold_candidates(rows, prep, logistic):
        selected = [row for row in rows if predict_probability(row, prep, logistic) <= threshold]
        coverage = len(selected) / len(rows) if rows else 0.0
        if coverage + 1e-12 < minimum_coverage or not selected:
            continue
        shares = sum(float(row["filled_shares"]) for row in selected)
        markout = sum(float(row["markout_per_share"]) * float(row["filled_shares"]) for row in selected) / shares if shares else -math.inf
        adverse_rate = sum(int(row["adverse"]) for row in selected) / len(selected)
        objective = (markout, -adverse_rate, coverage, len(selected))
        if best is None or objective > best:
            best = objective
            best_report = {"threshold": threshold, "coverage": coverage, "rows": len(selected), "filled_shares": shares, "share_weighted_markout_per_share": markout, "adverse_rate": adverse_rate}
    if best is None:
        return 0.25, {"threshold": 0.25, "state": "FALLBACK_FIXED_THRESHOLD_INSUFFICIENT_VALIDATION_SELECTION"}
    return float(best_report["threshold"]), best_report


def selected_metrics(rows: list[dict[str, Any]], prep: dict[str, dict[str, float]], logistic: list[float], threshold: float) -> dict[str, Any]:
    selected = [row for row in rows if predict_probability(row, prep, logistic) <= threshold]
    if not selected:
        return {"rows": 0, "coverage": 0.0, "clusters": 0}
    shares = sum(float(row["filled_shares"]) for row in selected)
    weighted = sum(float(row["markout_per_share"]) * float(row["filled_shares"]) for row in selected) / shares if shares else None
    return {
        "rows": len(selected),
        "coverage": len(selected) / len(rows) if rows else 0.0,
        "clusters": len({row["event_cluster"] for row in selected}),
        "filled_shares": shares,
        "adverse_rate": sum(int(row["adverse"]) for row in selected) / len(selected),
        "share_weighted_markout_per_share": weighted,
    }


def cluster_bootstrap_selected(rows: list[dict[str, Any]], prep: dict[str, dict[str, float]], logistic: list[float], threshold: float, *, samples: int, seed: int) -> dict[str, Any]:
    selected = [row for row in rows if predict_probability(row, prep, logistic) <= threshold]
    groups: defaultdict[str, list[dict[str, Any]]] = defaultdict(list)
    for row in selected:
        groups[str(row["event_cluster"])].append(row)
    names = sorted(groups)
    if not names:
        return {"clusters": 0, "bootstrap_samples": 0, "ci95": [None, None], "leave_best_cluster_out": None}
    cluster_stats = {
        name: (
            sum(float(row["markout_per_share"]) * float(row["filled_shares"]) for row in group),
            sum(float(row["filled_shares"]) for row in group),
        ) for name, group in groups.items()
    }
    rng = random.Random(seed)
    bootstrap: list[float] = []
    for _ in range(max(0, samples)):
        picked = [rng.choice(names) for _ in names]
        denominator = sum(cluster_stats[name][1] for name in picked)
        if denominator:
            bootstrap.append(sum(cluster_stats[name][0] for name in picked) / denominator)
    best = max(names, key=lambda name: cluster_stats[name][0] / max(cluster_stats[name][1], 1e-12))
    remaining_den = sum(value[1] for name, value in cluster_stats.items() if name != best)
    leave_best = sum(value[0] for name, value in cluster_stats.items() if name != best) / remaining_den if remaining_den else None
    return {
        "clusters": len(names),
        "bootstrap_samples": len(bootstrap),
        "ci95": [quantile(bootstrap, 0.025) if bootstrap else None, quantile(bootstrap, 0.975) if bootstrap else None],
        "leave_best_cluster_out": leave_best,
        "best_cluster": best,
    }


def coefficient_map(beta: list[float]) -> dict[str, float]:
    return {"intercept": beta[0], **{name: beta[index + 1] for index, name in enumerate(FEATURES)}}


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--maker-evidence", type=pathlib.Path, action="append", required=True)
    parser.add_argument("--output", type=pathlib.Path, required=True)
    parser.add_argument("--model-output", type=pathlib.Path)
    parser.add_argument("--markout-horizon", default="250ms")
    parser.add_argument("--placement-action", action="append", default=[])
    parser.add_argument("--include-bootstrap-probes", action="store_true")
    parser.add_argument("--minimum-clusters", type=int, default=20)
    parser.add_argument("--minimum-validation-coverage", type=float, default=0.15)
    parser.add_argument("--ridge", type=float, default=2.0)
    parser.add_argument("--iterations", type=int, default=2500)
    parser.add_argument("--bootstrap-samples", type=int, default=5000)
    parser.add_argument("--seed", type=int, default=2909)
    args = parser.parse_args()
    if args.minimum_clusters < 3 or not 0 < args.minimum_validation_coverage <= 1:
        raise SystemExit("invalid toxicity-model evidence gates")
    if not math.isfinite(args.ridge) or args.ridge < 0 or args.iterations <= 0 or args.bootstrap_samples < 0:
        raise SystemExit("invalid toxicity-model training arguments")
    actions = {str(value).upper() for value in args.placement_action} or {"JOIN", "IMPROVE1"}
    fill_rows, diagnostics = build_fill_rows(
        records(args.maker_evidence),
        markout_horizon=str(args.markout_horizon),
        placement_actions=actions,
        include_bootstrap_probes=args.include_bootstrap_probes,
    )
    clusters = {row["event_cluster"] for row in fill_rows}
    base = {
        "schema": REPORT_SCHEMA,
        "paper_only": True,
        "authenticated_execution": False,
        "real_order_submission": False,
        "execution_authority": "ZERO_AUTHORITY_RESEARCH_ONLY",
        "automatic_promotion": False,
        "target_probability": "P(MARKOUT_LT_0 | CANONICAL_PAPER_FILL)",
        "target_regression": f"E({args.markout_horizon}_MARKOUT_PER_SHARE | CANONICAL_PAPER_FILL)",
        "feature_cut": "ORDER_SUBMITTED_CAUSAL_PLACEMENT_FEATURES_ONLY",
        "labels": "FILL_AND_POST_FILL_MARKOUT_ONLY_NEVER_FEATURES",
        "markout_horizon": str(args.markout_horizon),
        "placement_actions": sorted(actions),
        "maker_diagnostics": diagnostics,
        "eligible_fill_rows": len(fill_rows),
        "independent_fill_clusters": len(clusters),
    }
    if len(clusters) < args.minimum_clusters:
        report = {**base, "state": "INSUFFICIENT_FILL_CONDITIONED_EVIDENCE", "minimum_clusters": args.minimum_clusters}
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(json.dumps(report, indent=2, sort_keys=True) + "\n", encoding="utf-8")
        print(json.dumps(report, indent=2, sort_keys=True))
        return 0

    train, validation, test, split = chronological_cluster_split(fill_rows)
    prep = fit_preprocessor(train)
    logistic = fit_logistic(train, prep, ridge=args.ridge, iterations=args.iterations)
    linear = fit_ridge_markout(train, prep, ridge=args.ridge)
    threshold, validation_selection = choose_safe_threshold(
        validation, prep, logistic, args.minimum_validation_coverage)
    test_selection = selected_metrics(test, prep, logistic, threshold)
    inference = cluster_bootstrap_selected(
        test, prep, logistic, threshold, samples=args.bootstrap_samples, seed=args.seed)
    model_core = {
        "schema": MODEL_SCHEMA,
        "paper_only": True,
        "authenticated_execution": False,
        "real_order_submission": False,
        "execution_authority": "ZERO_AUTHORITY_RESEARCH_ONLY",
        "automatic_promotion": False,
        "feature_names": list(FEATURES),
        "preprocessing": prep,
        "adverse_probability_coefficients": coefficient_map(logistic),
        "markout_regression_coefficients": coefficient_map(linear),
        "safe_to_quote_adverse_probability_threshold": threshold,
        "threshold_selection_role": "VALIDATION_ONLY_TEST_UNTOUCHED",
        "markout_horizon": str(args.markout_horizon),
        "placement_actions": sorted(actions),
        "split_cluster_sha256": {name: sha256(values) for name, values in split.items()},
    }
    model = {**model_core, "model_hash": sha256(model_core)}
    report = {
        **base,
        "state": "FIT_COMPLETE_ZERO_AUTHORITY",
        "split": {name: {"clusters": len(values), "cluster_sha256": sha256(values)} for name, values in split.items()},
        "feature_coverage_train": {name: prep[name]["observed"] / len(train) if train else 0.0 for name in FEATURES},
        "metrics": {
            "train": summarize(train, prep, logistic, linear),
            "validation": summarize(validation, prep, logistic, linear),
            "test": summarize(test, prep, logistic, linear),
        },
        "validation_safe_threshold_selection": validation_selection,
        "test_safe_to_quote": test_selection,
        "test_safe_to_quote_inference": inference,
        "model_hash": model["model_hash"],
        "promotion_gate": "NONE_RESEARCH_ONLY_REQUIRES_NEW_FORWARD_PAPER_REPLICATIONS",
    }
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(report, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    if args.model_output is not None:
        args.model_output.parent.mkdir(parents=True, exist_ok=True)
        args.model_output.write_text(json.dumps(model, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    print(json.dumps(report, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
