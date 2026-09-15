#!/usr/bin/env python3
"""Causal pre-fill path toxicity research for the PAPER Maker.

For each canonical fill, this script observes book/feature updates only until
`fill_time - cancel_latency`. The post-cut interval and all post-fill data are labels
only. The goal is a dynamic `P(adverse markout | eventual fill, causal live path)`
for shadow CANCEL/re-entry research. Outputs have zero execution authority.
"""
from __future__ import annotations

import argparse
import bisect
import json
import math
import pathlib
import random
import sys
from collections import defaultdict
from typing import Any

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))
import v7_maker_fill_conditioned_toxicity as base

SCHEMA = "polymarket_v7_maker_prefill_path_toxicity_v1"
MODEL_SCHEMA = "polymarket_v7_maker_prefill_path_toxicity_model_v1"
PATH_FEATURES = (
    "micro_score_available",
    "entry_micro_score",
    "last_micro_score",
    "minimum_micro_score",
    "micro_score_deterioration",
    "micro_score_recovery",
    "minimum_imbalance",
    "imbalance_deterioration",
    "minimum_ofi",
    "maximum_cancel_intensity",
    "maximum_trade_intensity",
    "maximum_sell_print_rate",
    "last_sell_age_entry_ms",
    "last_sell_age_cut_ms",
    "queue_ahead",
    "fill_probability",
    "path_observations",
    "path_duration_ms",
)


def num(value: Any, default: float = math.nan) -> float:
    return base.num(value, default)


def token_id(order: dict[str, Any]) -> str:
    direct = str(order.get("token_id") or "")
    if direct:
        return direct
    meta = base.metadata(order)
    envelope = meta.get("opportunity_envelope") if isinstance(meta.get("opportunity_envelope"), dict) else {}
    plan = envelope.get("execution_plan") if isinstance(envelope.get("execution_plan"), dict) else {}
    legs = plan.get("legs") if isinstance(plan.get("legs"), list) else []
    if len(legs) == 1 and isinstance(legs[0], dict):
        return str(legs[0].get("token_id") or "")
    return ""


def valid_book(row: dict[str, Any]) -> bool:
    return (
        row.get("schema") == "polymarket_v7_causal_book_observation_v1"
        and row.get("paper_only") is True
        and row.get("authenticated_execution") is False
        and row.get("real_order_submission") is False
        and row.get("execution_authority") == "ZERO_AUTHORITY_RESEARCH_ONLY"
        and row.get("valid") is True
        and row.get("lineage_continuous") is True
        and int(num(row.get("receive_wall_ms"), 0)) > 0
        and str(row.get("market_id") or "")
        and str(row.get("token_id") or "")
    )


def index_books(rows: list[dict[str, Any]]) -> tuple[
    dict[tuple[str, str], list[dict[str, Any]]],
    dict[tuple[str, str], list[int]],
]:
    books: defaultdict[tuple[str, str], list[dict[str, Any]]] = defaultdict(list)
    sells: defaultdict[tuple[str, str], list[int]] = defaultdict(list)
    for row in rows:
        if not valid_book(row):
            continue
        key = (str(row.get("market_id")), str(row.get("token_id")))
        books[key].append(row)
        trade = row.get("public_trade") if isinstance(row.get("public_trade"), dict) else None
        if trade and str(trade.get("aggressor_side") or "").upper() == "SELL":
            sells[key].append(int(num(row.get("receive_wall_ms"), 0)))
    for key in books:
        books[key].sort(key=lambda row: (
            int(num(row.get("receive_wall_ms"), 0)),
            int(num(row.get("observer_sequence"), 0)),
        ))
    for key in sells:
        sells[key].sort()
    return dict(books), dict(sells)


def order_map(rows: list[dict[str, Any]]) -> dict[tuple[str, str], dict[str, Any]]:
    result: dict[tuple[str, str], dict[str, Any]] = {}
    for row in rows:
        if row.get("event_type") == "ORDER_SUBMITTED" and row.get("order_id"):
            result[base.life(row)] = row
    return result


def latest_age(times: list[int], timestamp_ms: int) -> float:
    position = bisect.bisect_right(times, timestamp_ms)
    if position <= 0:
        return math.nan
    return float(max(0, timestamp_ms - times[position - 1]))


def path_value(row: dict[str, Any], name: str) -> float:
    features = row.get("placement_features") if isinstance(row.get("placement_features"), dict) else {}
    return num(features.get(name))


def summarize_path(
    fill_row: dict[str, Any], order: dict[str, Any],
    book_rows: list[dict[str, Any]], sell_times: list[int], *, cancel_latency_ms: float,
) -> dict[str, Any] | None:
    order_ms = int(fill_row["order_ts_ms"])
    fill_ms = int(fill_row["fill_ts_ms"])
    cutoff_ms = int(math.floor(fill_ms - cancel_latency_ms))
    if cutoff_ms <= order_ms:
        return None
    path = [
        row for row in book_rows
        if order_ms <= int(num(row.get("receive_wall_ms"), 0)) <= cutoff_ms
    ]
    if not path:
        return None
    entry = fill_row["features"]
    entry_score = num(entry.get("microstructure_shadow_delta_250ms"))
    entry_imbalance = num(entry.get("imbalance"))
    scores = [path_value(row, "microstructure_shadow_delta_250ms") for row in path]
    finite_scores = [(index, value) for index, value in enumerate(scores) if math.isfinite(value)]
    imbalances = [path_value(row, "imbalance") for row in path]
    ofis = [path_value(row, "ofi") for row in path]
    cancels = [path_value(row, "cancel_intensity") for row in path]
    trades = [path_value(row, "trade_intensity") for row in path]
    sell_rates = [path_value(row, "aggressive_sell_prints_per_second") for row in path]

    last_score = finite_scores[-1][1] if finite_scores else math.nan
    minimum_score = min((value for _, value in finite_scores), default=math.nan)
    if math.isfinite(entry_score) and math.isfinite(minimum_score):
        deterioration = max(0.0, entry_score - minimum_score)
    elif finite_scores:
        deterioration = max(0.0, finite_scores[0][1] - minimum_score)
    else:
        deterioration = math.nan
    recovery = math.nan
    if finite_scores:
        min_index, min_value = min(finite_scores, key=lambda item: item[1])
        after = [value for index, value in finite_scores if index >= min_index]
        recovery = max(after) - min_value if after else 0.0

    finite_imbalances = [value for value in imbalances if math.isfinite(value)]
    min_imbalance = min(finite_imbalances, default=math.nan)
    imbalance_deterioration = (
        max(0.0, entry_imbalance - min_imbalance)
        if math.isfinite(entry_imbalance) and math.isfinite(min_imbalance)
        else math.nan
    )
    features = {
        "micro_score_available": 1.0 if finite_scores or math.isfinite(entry_score) else 0.0,
        "entry_micro_score": entry_score,
        "last_micro_score": last_score,
        "minimum_micro_score": minimum_score,
        "micro_score_deterioration": deterioration,
        "micro_score_recovery": recovery,
        "minimum_imbalance": min_imbalance,
        "imbalance_deterioration": imbalance_deterioration,
        "minimum_ofi": min((value for value in ofis if math.isfinite(value)), default=math.nan),
        "maximum_cancel_intensity": max((value for value in cancels if math.isfinite(value)), default=math.nan),
        "maximum_trade_intensity": max((value for value in trades if math.isfinite(value)), default=math.nan),
        "maximum_sell_print_rate": max((value for value in sell_rates if math.isfinite(value)), default=math.nan),
        "last_sell_age_entry_ms": latest_age(sell_times, order_ms),
        "last_sell_age_cut_ms": latest_age(sell_times, cutoff_ms),
        "queue_ahead": num(entry.get("queue_ahead")),
        "fill_probability": num(entry.get("fill_probability")),
        "path_observations": float(len(path)),
        "path_duration_ms": float(cutoff_ms - order_ms),
    }
    return {
        **fill_row,
        "token_id": token_id(order),
        "cancel_latency_ms": cancel_latency_ms,
        "causal_cutoff_ms": cutoff_ms,
        "prefill_features": features,
        "path_first_receive_ms": int(num(path[0].get("receive_wall_ms"), 0)),
        "path_last_receive_ms": int(num(path[-1].get("receive_wall_ms"), 0)),
    }


def build_path_rows(
    maker_rows: list[dict[str, Any]], book_rows: list[dict[str, Any]], *,
    markout_horizon: str, placement_actions: set[str], cancel_latency_ms: float,
) -> tuple[list[dict[str, Any]], dict[str, int]]:
    fills, diagnostics = base.build_fill_rows(
        maker_rows,
        markout_horizon=markout_horizon,
        placement_actions=placement_actions,
        include_bootstrap_probes=False,
    )
    orders = order_map(maker_rows)
    books, sells = index_books(book_rows)
    output: list[dict[str, Any]] = []
    missing_order = missing_token = missing_path = 0
    for fill_row in fills:
        order = orders.get((str(fill_row["source_model_sha"]), str(fill_row["order_id"])))
        if order is None:
            missing_order += 1
            continue
        token = token_id(order)
        if not token:
            missing_token += 1
            continue
        key = (str(fill_row["market_id"]), token)
        row = summarize_path(
            fill_row, order, books.get(key, []), sells.get(key, []),
            cancel_latency_ms=cancel_latency_ms,
        )
        if row is None:
            missing_path += 1
            continue
        output.append(row)
    diagnostics = {
        **diagnostics,
        "fills_with_prefill_path": len(output),
        "fills_missing_order": missing_order,
        "fills_missing_token": missing_token,
        "fills_missing_causal_prefill_path": missing_path,
    }
    return output, diagnostics


def quantile(values: list[float], probability: float) -> float:
    return base.quantile(values, probability)


def fit_preprocessor(rows: list[dict[str, Any]]) -> dict[str, dict[str, float]]:
    result: dict[str, dict[str, float]] = {}
    for name in PATH_FEATURES:
        values = [num(row["prefill_features"].get(name)) for row in rows]
        values = [value for value in values if math.isfinite(value)]
        if not values:
            result[name] = {"lower": 0.0, "upper": 0.0, "median": 0.0, "scale": 1.0, "observed": 0}
            continue
        lower, upper = quantile(values, 0.02), quantile(values, 0.98)
        median = quantile(values, 0.50)
        scale = quantile(values, 0.90) - quantile(values, 0.10)
        if not math.isfinite(scale) or abs(scale) < 1e-9:
            scale = max(abs(median), 1.0)
        result[name] = {"lower": lower, "upper": upper, "median": median, "scale": scale, "observed": len(values)}
    return result


def transform(row: dict[str, Any], prep: dict[str, dict[str, float]]) -> list[float]:
    values = [1.0]
    for name in PATH_FEATURES:
        spec = prep[name]
        value = num(row["prefill_features"].get(name), spec["median"])
        if not math.isfinite(value):
            value = spec["median"]
        value = min(spec["upper"], max(spec["lower"], value))
        values.append((value - spec["median"]) / spec["scale"])
    return values


def cluster_weights(rows: list[dict[str, Any]]) -> list[float]:
    counts: defaultdict[str, int] = defaultdict(int)
    for row in rows:
        counts[str(row["event_cluster"])] += 1
    weights = [1.0 / counts[str(row["event_cluster"])] for row in rows]
    total = sum(weights)
    return [weight * len(rows) / total for weight in weights] if total else [1.0] * len(rows)


def sigmoid(value: float) -> float:
    value = max(-35.0, min(35.0, value))
    return 1.0 / (1.0 + math.exp(-value))


def fit_logistic(rows: list[dict[str, Any]], prep: dict[str, dict[str, float]], *, ridge: float, iterations: int) -> list[float]:
    width = len(PATH_FEATURES) + 1
    beta = [0.0] * width
    design = [transform(row, prep) for row in rows]
    labels = [float(row["adverse"]) for row in rows]
    weights = cluster_weights(rows)
    denominator = max(1.0, sum(weights))
    for iteration in range(max(1, iterations)):
        gradient = [0.0] * width
        for x, label, weight in zip(design, labels, weights):
            error = (sigmoid(sum(b * value for b, value in zip(beta, x))) - label) * weight
            for index, value in enumerate(x):
                gradient[index] += error * value
        for index in range(width):
            gradient[index] /= denominator
            if index:
                gradient[index] += ridge * beta[index] / max(1.0, len(rows))
        rate = 0.20 / math.sqrt(1.0 + iteration / 100.0)
        step = max(abs(value) for value in gradient)
        for index in range(width):
            beta[index] -= rate * gradient[index]
        if step < 1e-7:
            break
    return beta


def predict(row: dict[str, Any], prep: dict[str, dict[str, float]], beta: list[float]) -> float:
    return sigmoid(sum(coefficient * value for coefficient, value in zip(beta, transform(row, prep))))


def auc(rows: list[dict[str, Any]], prep: dict[str, dict[str, float]], beta: list[float]) -> float | None:
    labels = [int(row["adverse"]) for row in rows]
    scores = [predict(row, prep, beta) for row in rows]
    return base.auc(labels, scores)


def choose_threshold(rows: list[dict[str, Any]], prep: dict[str, dict[str, float]], beta: list[float], minimum_coverage: float) -> tuple[float, dict[str, Any]]:
    candidates = sorted(set([0.05, 0.10, 0.15, 0.20, 0.25, 0.30, 0.40, 0.50] + [round(predict(row, prep, beta), 6) for row in rows]))
    best = None
    report: dict[str, Any] = {}
    for threshold in candidates:
        kept = [row for row in rows if predict(row, prep, beta) <= threshold]
        coverage = len(kept) / len(rows) if rows else 0.0
        if coverage + 1e-12 < minimum_coverage or not kept:
            continue
        shares = sum(float(row["filled_shares"]) for row in kept)
        markout = sum(float(row["markout_per_share"]) * float(row["filled_shares"]) for row in kept) / shares if shares else -math.inf
        adverse = sum(int(row["adverse"]) for row in kept) / len(kept)
        objective = (markout, -adverse, coverage)
        if best is None or objective > best:
            best = objective
            report = {"threshold": threshold, "coverage": coverage, "rows": len(kept), "filled_shares": shares, "share_weighted_markout_per_share": markout, "adverse_rate": adverse}
    return (float(report["threshold"]), report) if report else (0.25, {"threshold": 0.25, "state": "FALLBACK_FIXED_THRESHOLD"})


def selected_metrics(rows: list[dict[str, Any]], prep: dict[str, dict[str, float]], beta: list[float], threshold: float) -> dict[str, Any]:
    kept = [row for row in rows if predict(row, prep, beta) <= threshold]
    if not kept:
        return {"rows": 0, "coverage": 0.0, "clusters": 0}
    shares = sum(float(row["filled_shares"]) for row in kept)
    return {
        "rows": len(kept),
        "coverage": len(kept) / len(rows),
        "clusters": len({row["event_cluster"] for row in kept}),
        "filled_shares": shares,
        "adverse_rate": sum(int(row["adverse"]) for row in kept) / len(kept),
        "share_weighted_markout_per_share": sum(float(row["markout_per_share"]) * float(row["filled_shares"]) for row in kept) / shares if shares else None,
    }


def bootstrap(rows: list[dict[str, Any]], prep: dict[str, dict[str, float]], beta: list[float], threshold: float, *, samples: int, seed: int) -> dict[str, Any]:
    kept = [row for row in rows if predict(row, prep, beta) <= threshold]
    groups: defaultdict[str, list[dict[str, Any]]] = defaultdict(list)
    for row in kept:
        groups[str(row["event_cluster"])].append(row)
    names = sorted(groups)
    stats = {
        name: (
            sum(float(row["markout_per_share"]) * float(row["filled_shares"]) for row in group),
            sum(float(row["filled_shares"]) for row in group),
        ) for name, group in groups.items()
    }
    rng = random.Random(seed)
    values: list[float] = []
    for _ in range(max(0, samples)):
        picked = [rng.choice(names) for _ in names] if names else []
        denominator = sum(stats[name][1] for name in picked)
        if denominator:
            values.append(sum(stats[name][0] for name in picked) / denominator)
    best = max(names, key=lambda name: stats[name][0] / max(stats[name][1], 1e-12), default=None)
    remaining = sum(stats[name][1] for name in names if name != best)
    leave_best = sum(stats[name][0] for name in names if name != best) / remaining if remaining else None
    return {
        "clusters": len(names),
        "bootstrap_samples": len(values),
        "ci95": [quantile(values, 0.025) if values else None, quantile(values, 0.975) if values else None],
        "leave_best_cluster_out": leave_best,
        "best_cluster": best,
    }


def coefficient_map(beta: list[float]) -> dict[str, float]:
    return {"intercept": beta[0], **{name: beta[index + 1] for index, name in enumerate(PATH_FEATURES)}}


def fit_one_latency(rows: list[dict[str, Any]], *, minimum_clusters: int, minimum_coverage: float, ridge: float, iterations: int, bootstrap_samples: int, seed: int) -> dict[str, Any]:
    clusters = {row["event_cluster"] for row in rows}
    if len(clusters) < minimum_clusters:
        return {"state": "INSUFFICIENT_PREFILL_PATH_EVIDENCE", "rows": len(rows), "clusters": len(clusters), "minimum_clusters": minimum_clusters}
    train, validation, test, split = base.chronological_cluster_split(rows)
    prep = fit_preprocessor(train)
    beta = fit_logistic(train, prep, ridge=ridge, iterations=iterations)
    threshold, validation_selection = choose_threshold(validation, prep, beta, minimum_coverage)
    model_core = {
        "schema": MODEL_SCHEMA,
        "paper_only": True,
        "authenticated_execution": False,
        "real_order_submission": False,
        "execution_authority": "ZERO_AUTHORITY_RESEARCH_ONLY",
        "automatic_promotion": False,
        "feature_names": list(PATH_FEATURES),
        "preprocessing": prep,
        "adverse_probability_coefficients": coefficient_map(beta),
        "safe_to_keep_adverse_probability_threshold": threshold,
        "threshold_role": "VALIDATION_ONLY_TEST_UNTOUCHED",
        "cancel_latency_ms": rows[0]["cancel_latency_ms"] if rows else None,
        "split_cluster_sha256": {name: base.sha256(values) for name, values in split.items()},
    }
    model = {**model_core, "model_hash": base.sha256(model_core)}
    return {
        "state": "FIT_COMPLETE_ZERO_AUTHORITY",
        "model": model,
        "split": {name: {"clusters": len(values), "sha256": base.sha256(values)} for name, values in split.items()},
        "metrics": {
            "train_auc": auc(train, prep, beta),
            "validation_auc": auc(validation, prep, beta),
            "test_auc": auc(test, prep, beta),
        },
        "validation_safe_to_keep": validation_selection,
        "test_safe_to_keep": selected_metrics(test, prep, beta, threshold),
        "test_inference": bootstrap(test, prep, beta, threshold, samples=bootstrap_samples, seed=seed),
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--maker-evidence", type=pathlib.Path, action="append", required=True)
    parser.add_argument("--book-evidence", type=pathlib.Path, action="append", required=True)
    parser.add_argument("--output", type=pathlib.Path, required=True)
    parser.add_argument("--markout-horizon", default="250ms")
    parser.add_argument("--placement-action", action="append", default=[])
    parser.add_argument("--cancel-latency-ms", default="5,25,50,100")
    parser.add_argument("--minimum-clusters", type=int, default=20)
    parser.add_argument("--minimum-validation-coverage", type=float, default=0.15)
    parser.add_argument("--ridge", type=float, default=2.0)
    parser.add_argument("--iterations", type=int, default=2500)
    parser.add_argument("--bootstrap-samples", type=int, default=5000)
    parser.add_argument("--seed", type=int, default=3109)
    args = parser.parse_args()
    actions = {str(value).upper() for value in args.placement_action} or {"JOIN", "IMPROVE1"}
    maker_rows = base.records(args.maker_evidence)
    book_rows = base.records(args.book_evidence)
    latencies = []
    for token in str(args.cancel_latency_ms).split(","):
        value = num(token.strip())
        if not math.isfinite(value) or value < 0:
            raise SystemExit("invalid --cancel-latency-ms")
        latencies.append(float(value))
    results = []
    for index, latency in enumerate(sorted(set(latencies))):
        path_rows, diagnostics = build_path_rows(
            maker_rows, book_rows,
            markout_horizon=str(args.markout_horizon),
            placement_actions=actions,
            cancel_latency_ms=latency,
        )
        fitted = fit_one_latency(
            path_rows,
            minimum_clusters=args.minimum_clusters,
            minimum_coverage=args.minimum_validation_coverage,
            ridge=args.ridge,
            iterations=args.iterations,
            bootstrap_samples=args.bootstrap_samples,
            seed=args.seed + index,
        )
        results.append({"cancel_latency_ms": latency, "diagnostics": diagnostics, **fitted})
    report = {
        "schema": SCHEMA,
        "paper_only": True,
        "authenticated_execution": False,
        "real_order_submission": False,
        "execution_authority": "ZERO_AUTHORITY_RESEARCH_ONLY",
        "automatic_promotion": False,
        "feature_cut": "BOOK_OBSERVATIONS_STRICTLY_AT_OR_BEFORE_FILL_MINUS_CANCEL_LATENCY",
        "labels": "POST_FILL_MARKOUT_ONLY",
        "markout_horizon": str(args.markout_horizon),
        "placement_actions": sorted(actions),
        "latency_models": results,
        "promotion_gate": "NONE_RESEARCH_ONLY_REQUIRES_FORWARD_SHADOW_AND_PAPER_REPLICATIONS",
    }
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(report, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    print(json.dumps(report, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
