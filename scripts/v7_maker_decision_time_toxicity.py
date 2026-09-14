#!/usr/bin/env python3
"""Decision-time Maker toxicity research with no fill-time oracle.

Each training row is anchored to an actually observed causal book update while a
PAPER maker order is still alive. Features use only information available at
that book timestamp. Future fills and markouts are labels only.

The target is the hazard that an *avoidable* adverse first fill occurs after an
assumed cancel latency and within a fixed forward window. Fills arriving before
cancel could become effective are recorded separately as unavoidable, not used
to select the decision timestamp. Outputs have zero execution authority.
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

SCHEMA = "polymarket_v7_maker_decision_time_toxicity_v1"
MODEL_SCHEMA = "polymarket_v7_maker_decision_time_toxicity_model_v1"
TERMINAL_STATES = {"CANCELLED", "EXPIRED", "REJECTED", "NONFILL", "FILLED"}
FEATURES = (
    "micro_score_available",
    "micro_score",
    "micro_score_change_from_entry",
    "minimum_micro_score_100ms",
    "micro_score_deterioration_100ms",
    "imbalance",
    "imbalance_change_from_entry",
    "minimum_imbalance_100ms",
    "ofi",
    "minimum_ofi_100ms",
    "maximum_cancel_intensity_100ms",
    "maximum_trade_intensity_100ms",
    "maximum_sell_print_rate_100ms",
    "last_sell_age_ms",
    "spread_ticks",
    "distance_from_touch_ticks",
    "queue_ahead",
    "fill_probability",
    "order_age_ms",
    "history_observations_100ms",
)


def num(value: Any, default: float = math.nan) -> float:
    return base.num(value, default)


def timestamp(row: dict[str, Any]) -> int:
    return int(num(row.get("recorded_ts_ms") or row.get("receive_ts_ms"), 0.0))


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


def canonical_order(row: dict[str, Any], placement_actions: set[str]) -> bool:
    if row.get("event_type") != "ORDER_SUBMITTED" or not row.get("order_id"):
        return False
    if row.get("paper_only") is not True or row.get("authenticated_execution") is not False:
        return False
    meta = base.metadata(row)
    semantics = str(meta.get("execution_semantics_version") or "")
    if meta.get("component") not in {None, "professional_maker"}:
        return False
    if semantics and semantics != base.SEMANTICS:
        return False
    if str(row.get("side") or meta.get("execution_side") or "").upper() != "BUY":
        return False
    if base.placement(row) not in placement_actions:
        return False
    if meta.get("paper_bootstrap_probe") is True:
        return False
    return timestamp(row) > 0 and bool(str(row.get("model_sha") or ""))


def valid_book(row: dict[str, Any]) -> bool:
    return (
        row.get("schema") == "polymarket_v7_causal_book_observation_v1"
        and row.get("paper_only") is True
        and row.get("authenticated_execution") is False
        and row.get("real_order_submission") is False
        and row.get("execution_authority") == "ZERO_AUTHORITY_RESEARCH_ONLY"
        and row.get("valid") is True
        and row.get("features_valid") is True
        and row.get("lineage_continuous") is True
        and int(num(row.get("receive_wall_ms"), 0.0)) > 0
        and bool(str(row.get("model_sha") or ""))
        and bool(str(row.get("market_id") or ""))
        and bool(str(row.get("token_id") or ""))
    )


def book_features(row: dict[str, Any]) -> dict[str, Any]:
    value = row.get("placement_features")
    return value if isinstance(value, dict) else {}


def index_books(rows: list[dict[str, Any]]) -> tuple[
    dict[tuple[str, str, str], list[dict[str, Any]]],
    dict[tuple[str, str, str, str, int], list[int]],
]:
    books: defaultdict[tuple[str, str, str], list[dict[str, Any]]] = defaultdict(list)
    sells: defaultdict[tuple[str, str, str, str, int], list[int]] = defaultdict(list)
    for row in rows:
        if not valid_book(row):
            continue
        sha = str(row.get("model_sha"))
        market = str(row.get("market_id"))
        token = str(row.get("token_id"))
        books[(sha, market, token)].append(row)
        trade = row.get("public_trade") if isinstance(row.get("public_trade"), dict) else None
        if trade and str(trade.get("aggressor_side") or "").upper() == "SELL":
            session = str(row.get("observer_session_id") or "")
            epoch = int(num(row.get("connection_epoch"), 0.0))
            sells[(sha, market, token, session, epoch)].append(int(num(row.get("receive_wall_ms"), 0.0)))
    for values in books.values():
        values.sort(key=lambda row: (
            int(num(row.get("receive_wall_ms"), 0.0)),
            int(num(row.get("observer_sequence"), 0.0)),
        ))
    for values in sells.values():
        values.sort()
    return dict(books), dict(sells)


def markout_map(rows: list[dict[str, Any]], horizon: str) -> dict[tuple[str, str, str], float]:
    result: dict[tuple[str, str, str], tuple[int, float]] = {}
    for row in rows:
        if row.get("event_type") != "MARKOUT" or not isinstance(row.get("markouts"), dict):
            continue
        if horizon not in row["markouts"]:
            continue
        value = num(row["markouts"].get(horizon))
        fill_id = str(row.get("fill_id") or "")
        order_id = str(row.get("order_id") or "")
        sha = str(row.get("model_sha") or "")
        when = timestamp(row)
        if not sha or not order_id or not fill_id or when <= 0 or not math.isfinite(value):
            continue
        key = (sha, order_id, fill_id)
        prior = result.get(key)
        if prior is None or when < prior[0]:
            result[key] = (when, value)
    return {key: value for key, (_, value) in result.items()}


def first_fill_map(rows: list[dict[str, Any]]) -> dict[tuple[str, str], dict[str, Any]]:
    result: dict[tuple[str, str], dict[str, Any]] = {}
    for row in rows:
        if row.get("event_type") != "FILL" or num(row.get("filled_size"), 0.0) <= 0:
            continue
        key = base.life(row)
        when = timestamp(row)
        if not key[0] or not key[1] or when <= 0:
            continue
        prior = result.get(key)
        if prior is None or when < timestamp(prior):
            result[key] = row
    return result


def terminal_map(rows: list[dict[str, Any]]) -> dict[tuple[str, str], int]:
    result: dict[tuple[str, str], int] = {}
    for row in rows:
        if row.get("event_type") != "ORDER_STATE":
            continue
        state = str(row.get("order_state") or "").upper()
        if state not in TERMINAL_STATES:
            continue
        key = base.life(row)
        when = timestamp(row)
        if not key[0] or not key[1] or when <= 0:
            continue
        result[key] = min(result.get(key, when), when)
    return result


def latest_age(times: list[int], current_ms: int) -> float:
    position = bisect.bisect_right(times, current_ms)
    if position <= 0:
        return math.nan
    return float(max(0, current_ms - times[position - 1]))


def rolling_values(
    history: list[dict[str, Any]], now_ms: int, window_ms: int,
    *, observer_session_id: str, connection_epoch: int,
) -> list[dict[str, Any]]:
    lower = now_ms - max(0, window_ms)
    return [
        row for row in history
        if lower <= int(num(row.get("receive_wall_ms"), 0.0)) <= now_ms
        and str(row.get("observer_session_id") or "") == observer_session_id
        and int(num(row.get("connection_epoch"), 0.0)) == connection_epoch
    ]


def feature_value(row: dict[str, Any], name: str) -> float:
    return num(book_features(row).get(name))


def decision_features(
    order: dict[str, Any],
    current: dict[str, Any],
    history: list[dict[str, Any]],
    sell_times: list[int],
    *,
    trailing_ms: int,
) -> dict[str, float]:
    now_ms = int(num(current.get("receive_wall_ms"), 0.0))
    entry = base.feature_cut(order)
    entry_score = num(entry.get("microstructure_shadow_delta_250ms"))
    entry_imbalance = num(entry.get("imbalance"))
    session = str(current.get("observer_session_id") or "")
    epoch = int(num(current.get("connection_epoch"), 0.0))
    recent = rolling_values(
        history, now_ms, trailing_ms,
        observer_session_id=session, connection_epoch=epoch,
    )
    scores = [feature_value(row, "microstructure_shadow_delta_250ms") for row in recent]
    scores = [value for value in scores if math.isfinite(value)]
    imbalances = [feature_value(row, "imbalance") for row in recent]
    imbalances = [value for value in imbalances if math.isfinite(value)]
    ofis = [feature_value(row, "ofi") for row in recent]
    ofis = [value for value in ofis if math.isfinite(value)]
    cancels = [feature_value(row, "cancel_intensity") for row in recent]
    cancels = [value for value in cancels if math.isfinite(value)]
    trades = [feature_value(row, "trade_intensity") for row in recent]
    trades = [value for value in trades if math.isfinite(value)]
    sell_rates = [feature_value(row, "aggressive_sell_prints_per_second") for row in recent]
    sell_rates = [value for value in sell_rates if math.isfinite(value)]

    score = feature_value(current, "microstructure_shadow_delta_250ms")
    imbalance = feature_value(current, "imbalance")
    return {
        "micro_score_available": 1.0 if math.isfinite(score) else 0.0,
        "micro_score": score,
        "micro_score_change_from_entry": score - entry_score if math.isfinite(score) and math.isfinite(entry_score) else math.nan,
        "minimum_micro_score_100ms": min(scores, default=math.nan),
        "micro_score_deterioration_100ms": max(scores) - score if scores and math.isfinite(score) else math.nan,
        "imbalance": imbalance,
        "imbalance_change_from_entry": imbalance - entry_imbalance if math.isfinite(imbalance) and math.isfinite(entry_imbalance) else math.nan,
        "minimum_imbalance_100ms": min(imbalances, default=math.nan),
        "ofi": feature_value(current, "ofi"),
        "minimum_ofi_100ms": min(ofis, default=math.nan),
        "maximum_cancel_intensity_100ms": max(cancels, default=math.nan),
        "maximum_trade_intensity_100ms": max(trades, default=math.nan),
        "maximum_sell_print_rate_100ms": max(sell_rates, default=math.nan),
        "last_sell_age_ms": latest_age(sell_times, now_ms),
        "spread_ticks": feature_value(current, "spread_ticks"),
        "distance_from_touch_ticks": feature_value(current, "distance_from_touch_ticks"),
        "queue_ahead": num(entry.get("queue_ahead")),
        "fill_probability": num(entry.get("fill_probability")),
        "order_age_ms": float(max(0, now_ms - timestamp(order))),
        "history_observations_100ms": float(len(recent)),
    }


def build_decision_rows(
    maker_rows: list[dict[str, Any]],
    book_rows: list[dict[str, Any]],
    *,
    markout_horizon: str,
    placement_actions: set[str],
    cancel_latency_ms: float,
    fill_hazard_window_ms: int,
    minimum_decision_spacing_ms: int,
    trailing_ms: int = 100,
) -> tuple[list[dict[str, Any]], dict[str, int]]:
    orders = {
        base.life(row): row
        for row in maker_rows
        if canonical_order(row, placement_actions)
    }
    fills = first_fill_map(maker_rows)
    terminals = terminal_map(maker_rows)
    markouts = markout_map(maker_rows, markout_horizon)
    books, sells = index_books(book_rows)

    evidence_end_ms = max(
        [timestamp(row) for row in maker_rows if timestamp(row) > 0]
        + [int(num(row.get("receive_wall_ms"), 0.0)) for row in book_rows if int(num(row.get("receive_wall_ms"), 0.0)) > 0]
        + [0]
    )
    output: list[dict[str, Any]] = []
    diagnostics = {
        "candidate_orders": len(orders),
        "orders_missing_token": 0,
        "orders_missing_book_path": 0,
        "decision_rows": 0,
        "decision_rows_with_first_fill_in_horizon": 0,
        "decision_rows_with_avoidable_adverse_fill": 0,
        "decision_rows_with_unavoidable_fill": 0,
        "decision_rows_missing_fill_markout": 0,
        "decision_rows_right_censored": 0,
    }

    for key, order in orders.items():
        sha, order_id = key
        market = str(order.get("market_id") or "")
        token = token_id(order)
        if not token:
            diagnostics["orders_missing_token"] += 1
            continue
        start_ms = timestamp(order)
        fill = fills.get(key)
        fill_ms = timestamp(fill) if fill is not None else 0
        terminal_ms = terminals.get(key, 0)
        life_end_candidates = [value for value in (fill_ms, terminal_ms) if value > start_ms]
        life_end_ms = min(life_end_candidates) if life_end_candidates else 0

        path = [
            row for row in books.get((sha, market, token), [])
            if int(num(row.get("receive_wall_ms"), 0.0)) >= start_ms
            and (life_end_ms <= 0 or int(num(row.get("receive_wall_ms"), 0.0)) < life_end_ms)
        ]
        if not path:
            diagnostics["orders_missing_book_path"] += 1
            continue

        markout = math.nan
        fill_id = ""
        fill_shares = 0.0
        if fill is not None:
            fill_id = str(fill.get("fill_id") or "")
            fill_shares = max(0.0, num(fill.get("filled_size"), 0.0))
            markout = markouts.get((sha, order_id, fill_id), math.nan)

        last_decision_ms = -10**30
        history: list[dict[str, Any]] = []
        for row in path:
            now_ms = int(num(row.get("receive_wall_ms"), 0.0))
            history.append(row)
            if now_ms - last_decision_ms < max(0, minimum_decision_spacing_ms):
                continue
            last_decision_ms = now_ms

            effective_cancel_ms = int(math.ceil(now_ms + max(0.0, cancel_latency_ms)))
            horizon_end_ms = effective_cancel_ms + max(1, int(fill_hazard_window_ms))
            fill_before_cancel = fill_ms > 0 and fill_ms <= effective_cancel_ms
            fill_in_window = fill_ms > effective_cancel_ms and fill_ms <= horizon_end_ms
            adverse = False
            favorable = False
            label_missing = False
            if fill_before_cancel:
                diagnostics["decision_rows_with_unavoidable_fill"] += 1
            elif fill_in_window:
                diagnostics["decision_rows_with_first_fill_in_horizon"] += 1
                if math.isfinite(markout):
                    adverse = markout < 0.0
                    favorable = markout > 0.0
                    if adverse:
                        diagnostics["decision_rows_with_avoidable_adverse_fill"] += 1
                else:
                    label_missing = True
                    diagnostics["decision_rows_missing_fill_markout"] += 1
            elif fill_ms <= 0 and terminal_ms <= 0 and evidence_end_ms < horizon_end_ms:
                label_missing = True
                diagnostics["decision_rows_right_censored"] += 1

            session = str(row.get("observer_session_id") or "")
            epoch = int(num(row.get("connection_epoch"), 0.0))
            sell_key = (sha, market, token, session, epoch)
            features = decision_features(
                order,
                row,
                history,
                sells.get(sell_key, []),
                trailing_ms=trailing_ms,
            )
            output.append({
                "source_model_sha": sha,
                "order_id": order_id,
                "order_ts_ms": start_ms,
                "market_id": market,
                "event_cluster": str(order.get("event_id") or market or "UNKNOWN"),
                "token_id": token,
                "decision_ts_ms": now_ms,
                "decision_snapshot_id": str(row.get("observer_sequence") or ""),
                "observer_session_id": session,
                "connection_epoch": epoch,
                "cancel_latency_ms": float(cancel_latency_ms),
                "fill_hazard_window_ms": int(fill_hazard_window_ms),
                "effective_cancel_ms": effective_cancel_ms,
                "first_fill_ts_ms": fill_ms or None,
                "terminal_ts_ms": terminal_ms or None,
                "unavoidable_fill_before_cancel_effective": 1 if fill_before_cancel else 0,
                "first_fill_in_horizon": 1 if fill_in_window else 0,
                "avoidable_adverse_fill": 1 if adverse else 0,
                "avoidable_favorable_fill": 1 if favorable else 0,
                "label_complete": not label_missing,
                "first_fill_id": fill_id,
                "first_fill_shares": fill_shares,
                "first_fill_markout_per_share": markout if math.isfinite(markout) else None,
                "features": features,
            })

    diagnostics["decision_rows"] = len(output)
    return output, diagnostics


def supervised_rows(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return [row for row in rows if row.get("label_complete") is True]


def fit_preprocessor(rows: list[dict[str, Any]]) -> dict[str, dict[str, float]]:
    result: dict[str, dict[str, float]] = {}
    for name in FEATURES:
        values = [num(row["features"].get(name)) for row in rows]
        values = [value for value in values if math.isfinite(value)]
        if not values:
            result[name] = {"lower": 0.0, "upper": 0.0, "median": 0.0, "scale": 1.0, "observed": 0}
            continue
        lower, upper = base.quantile(values, 0.02), base.quantile(values, 0.98)
        median = base.quantile(values, 0.50)
        scale = base.quantile(values, 0.90) - base.quantile(values, 0.10)
        if not math.isfinite(scale) or abs(scale) < 1e-9:
            scale = max(abs(median), 1.0)
        result[name] = {"lower": lower, "upper": upper, "median": median, "scale": scale, "observed": len(values)}
    return result


def transform(row: dict[str, Any], prep: dict[str, dict[str, float]]) -> list[float]:
    values = [1.0]
    for name in FEATURES:
        spec = prep[name]
        value = num(row["features"].get(name), spec["median"])
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


def fit_logistic(
    rows: list[dict[str, Any]],
    prep: dict[str, dict[str, float]],
    *,
    target: str,
    ridge: float,
    iterations: int,
) -> list[float]:
    width = len(FEATURES) + 1
    beta = [0.0] * width
    design = [transform(row, prep) for row in rows]
    labels = [float(row[target]) for row in rows]
    weights = cluster_weights(rows)
    denominator = max(1.0, sum(weights))
    for iteration in range(max(1, iterations)):
        gradient = [0.0] * width
        for x, label, weight in zip(design, labels, weights):
            prediction = sigmoid(sum(coef * value for coef, value in zip(beta, x)))
            error = (prediction - label) * weight
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
    return sigmoid(sum(coef * value for coef, value in zip(beta, transform(row, prep))))


def auc(rows: list[dict[str, Any]], prep: dict[str, dict[str, float]], beta: list[float], target: str) -> float | None:
    return base.auc([int(row[target]) for row in rows], [predict(row, prep, beta) for row in rows])


def brier(rows: list[dict[str, Any]], prep: dict[str, dict[str, float]], beta: list[float], target: str) -> float | None:
    if not rows:
        return None
    return sum((predict(row, prep, beta) - float(row[target])) ** 2 for row in rows) / len(rows)


def coefficient_map(beta: list[float]) -> dict[str, float]:
    return {"intercept": beta[0], **{name: beta[index + 1] for index, name in enumerate(FEATURES)}}


def risk_buckets(rows: list[dict[str, Any]], prep: dict[str, dict[str, float]], beta: list[float], target: str) -> list[dict[str, Any]]:
    scored = sorted((predict(row, prep, beta), row) for row in rows)
    if not scored:
        return []
    buckets: list[dict[str, Any]] = []
    count = min(10, len(scored))
    for bucket in range(count):
        lo = bucket * len(scored) // count
        hi = (bucket + 1) * len(scored) // count
        values = scored[lo:hi]
        if not values:
            continue
        buckets.append({
            "bucket": bucket + 1,
            "rows": len(values),
            "mean_predicted_probability": sum(score for score, _ in values) / len(values),
            "observed_rate": sum(int(row[target]) for _, row in values) / len(values),
            "clusters": len({str(row["event_cluster"]) for _, row in values}),
        })
    return buckets


def fit_one_latency(
    rows: list[dict[str, Any]],
    *,
    minimum_clusters: int,
    ridge: float,
    iterations: int,
) -> dict[str, Any]:
    rows = supervised_rows(rows)
    clusters = {str(row["event_cluster"]) for row in rows}
    if len(clusters) < minimum_clusters:
        return {
            "state": "INSUFFICIENT_DECISION_TIME_EVIDENCE",
            "rows": len(rows),
            "clusters": len(clusters),
            "minimum_clusters": minimum_clusters,
        }
    train, validation, test, split = base.chronological_cluster_split(rows)
    prep = fit_preprocessor(train)
    beta_adverse = fit_logistic(
        train, prep, target="avoidable_adverse_fill", ridge=ridge, iterations=iterations
    )
    beta_fill = fit_logistic(
        train, prep, target="first_fill_in_horizon", ridge=ridge, iterations=iterations
    )
    model_core = {
        "schema": MODEL_SCHEMA,
        "paper_only": True,
        "authenticated_execution": False,
        "real_order_submission": False,
        "execution_authority": "ZERO_AUTHORITY_RESEARCH_ONLY",
        "automatic_promotion": False,
        "feature_names": list(FEATURES),
        "preprocessing": prep,
        "avoidable_adverse_fill_coefficients": coefficient_map(beta_adverse),
        "first_fill_hazard_coefficients": coefficient_map(beta_fill),
        "threshold": None,
        "threshold_role": "NONE_SCORE_REQUIRES_SEPARATE_FORWARD_SHADOW_POLICY_REPLAY",
        "cancel_latency_ms": rows[0]["cancel_latency_ms"] if rows else None,
        "fill_hazard_window_ms": rows[0]["fill_hazard_window_ms"] if rows else None,
        "split_cluster_sha256": {name: base.sha256(values) for name, values in split.items()},
    }
    return {
        "state": "FIT_COMPLETE_ZERO_AUTHORITY",
        "model": {**model_core, "model_hash": base.sha256(model_core)},
        "split": {
            name: {"clusters": len(values), "sha256": base.sha256(values)}
            for name, values in split.items()
        },
        "metrics": {
            "train_adverse_auc": auc(train, prep, beta_adverse, "avoidable_adverse_fill"),
            "validation_adverse_auc": auc(validation, prep, beta_adverse, "avoidable_adverse_fill"),
            "test_adverse_auc": auc(test, prep, beta_adverse, "avoidable_adverse_fill"),
            "test_adverse_brier": brier(test, prep, beta_adverse, "avoidable_adverse_fill"),
            "train_fill_auc": auc(train, prep, beta_fill, "first_fill_in_horizon"),
            "validation_fill_auc": auc(validation, prep, beta_fill, "first_fill_in_horizon"),
            "test_fill_auc": auc(test, prep, beta_fill, "first_fill_in_horizon"),
            "test_fill_brier": brier(test, prep, beta_fill, "first_fill_in_horizon"),
            "test_adverse_risk_buckets": risk_buckets(test, prep, beta_adverse, "avoidable_adverse_fill"),
        },
        "policy_gate": {
            "cancel_authority": False,
            "requires_event_driven_shadow_replay": True,
            "requires_forward_paper_replication": True,
        },
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--maker-evidence", type=pathlib.Path, action="append", required=True)
    parser.add_argument("--book-evidence", type=pathlib.Path, action="append", required=True)
    parser.add_argument("--output", type=pathlib.Path, required=True)
    parser.add_argument("--markout-horizon", default="250ms")
    parser.add_argument("--placement-action", action="append", default=[])
    parser.add_argument("--cancel-latency-ms", default="5,25,50,100")
    parser.add_argument("--fill-hazard-window-ms", type=int, default=500)
    parser.add_argument("--minimum-decision-spacing-ms", type=int, default=25)
    parser.add_argument("--trailing-feature-window-ms", type=int, default=100)
    parser.add_argument("--minimum-clusters", type=int, default=20)
    parser.add_argument("--ridge", type=float, default=2.0)
    parser.add_argument("--iterations", type=int, default=2500)
    args = parser.parse_args()

    actions = {str(value).upper() for value in args.placement_action} or {"JOIN", "IMPROVE1"}
    maker_rows = base.records(args.maker_evidence)
    book_rows = base.records(args.book_evidence)
    latencies: list[float] = []
    for raw in str(args.cancel_latency_ms).split(","):
        value = num(raw.strip())
        if not math.isfinite(value) or value < 0:
            raise SystemExit("invalid --cancel-latency-ms")
        latencies.append(float(value))
    if args.fill_hazard_window_ms <= 0 or args.minimum_decision_spacing_ms < 0:
        raise SystemExit("invalid decision-time horizon/spacing")

    results = []
    for latency in sorted(set(latencies)):
        decision_rows, diagnostics = build_decision_rows(
            maker_rows,
            book_rows,
            markout_horizon=str(args.markout_horizon),
            placement_actions=actions,
            cancel_latency_ms=latency,
            fill_hazard_window_ms=args.fill_hazard_window_ms,
            minimum_decision_spacing_ms=args.minimum_decision_spacing_ms,
            trailing_ms=args.trailing_feature_window_ms,
        )
        results.append({
            "cancel_latency_ms": latency,
            "diagnostics": diagnostics,
            **fit_one_latency(
                decision_rows,
                minimum_clusters=args.minimum_clusters,
                ridge=args.ridge,
                iterations=args.iterations,
            ),
        })

    report = {
        "schema": SCHEMA,
        "paper_only": True,
        "authenticated_execution": False,
        "real_order_submission": False,
        "execution_authority": "ZERO_AUTHORITY_RESEARCH_ONLY",
        "automatic_promotion": False,
        "decision_clock": "OBSERVED_BOOK_EVENT_TIMESTAMP_WHILE_ORDER_ALIVE",
        "feature_cut": "ONLY_DATA_AT_OR_BEFORE_DECISION_TIMESTAMP",
        "future_fill_time_role": "LABEL_ONLY_NEVER_USED_TO_CHOOSE_DECISION_TIMESTAMP",
        "target": "P(AVOIDABLE_ADVERSE_FIRST_FILL_WITHIN_FORWARD_WINDOW|CURRENT_CAUSAL_STATE)",
        "markout_horizon": str(args.markout_horizon),
        "fill_hazard_window_ms": args.fill_hazard_window_ms,
        "minimum_decision_spacing_ms": args.minimum_decision_spacing_ms,
        "trailing_feature_window_ms": args.trailing_feature_window_ms,
        "placement_actions": sorted(actions),
        "latency_models": results,
        "promotion_gate": "NONE_RESEARCH_ONLY_REQUIRES_EVENT_DRIVEN_SHADOW_REPLAY_AND_FORWARD_PAPER_REPLICATIONS",
    }
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(report, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    print(json.dumps(report, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
