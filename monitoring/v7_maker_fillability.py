#!/usr/bin/env python3
"""Exact-SHA, read-only fillability diagnostics for the V7 PAPER maker.

The module never changes queue semantics or generates orders. It combines the
canonical maker ledger with the independent taker-only public trade tape to
reconstruct a conservative fillability funnel. The REST tape is intentionally
classified as coarse diagnostic evidence (exchange timestamps are second
resolution); it is not allowed to prove a queue-simulator bug by itself.
"""
from __future__ import annotations

import csv
import hashlib
import json
import math
import threading
import time
from collections import Counter, defaultdict
from pathlib import Path
from typing import Any, Iterable

STRATEGY = "MICRO_MAKER_PRO"
DEFAULT_SUBMISSION_LATENCY_MS = 1.0
DEFAULT_EXPECTED_QUEUE_MULTIPLIER = 1.25
DEFAULT_UPPER_QUEUE_MULTIPLIER = 1.50
DEFAULT_MIN_QUOTE_LIFETIME_MS = 100.0
DEFAULT_EXPLORATION_MIN_REST_MS = 250.0
_EPS = 1e-12
_CACHE_LOCK = threading.Lock()
_CACHE_KEY: tuple[Any, ...] | None = None
_CACHE_VALUE: dict[str, Any] | None = None


def _number(value: Any, default: float | None = None) -> float | None:
    try:
        out = float(value)
    except (TypeError, ValueError, OverflowError):
        return default
    return out if math.isfinite(out) else default


def _integer(value: Any, default: int = 0) -> int:
    value_f = _number(value)
    return default if value_f is None else int(value_f)


def _metadata(row: dict[str, Any]) -> dict[str, Any]:
    value = row.get("metadata")
    return value if isinstance(value, dict) else {}


def _stat_key(path: Path) -> tuple[int, int]:
    try:
        stat = path.stat()
        return stat.st_size, stat.st_mtime_ns
    except OSError:
        return -1, -1


def _policy(path: Path | None) -> tuple[dict[str, Any], str]:
    if path is None:
        return {}, "missing"
    try:
        raw = path.read_bytes()
        value = json.loads(raw)
    except (OSError, json.JSONDecodeError):
        return {}, "invalid"
    return (value if isinstance(value, dict) else {}), hashlib.sha256(raw).hexdigest()


def _policy_values(root: dict[str, Any]) -> dict[str, float]:
    paper = root.get("paper_queue") if isinstance(root.get("paper_queue"), dict) else {}
    quoting = root.get("quoting") if isinstance(root.get("quoting"), dict) else {}
    exploration = root.get("exploration") if isinstance(root.get("exploration"), dict) else {}
    multipliers = paper.get("queue_ahead_multipliers") if isinstance(paper.get("queue_ahead_multipliers"), dict) else {}
    return {
        "submission_latency_ms": max(0.0, _number(paper.get("assumed_submission_latency_ms"), DEFAULT_SUBMISSION_LATENCY_MS) or DEFAULT_SUBMISSION_LATENCY_MS),
        "expected_queue_multiplier": max(1.0, _number(multipliers.get("expected"), DEFAULT_EXPECTED_QUEUE_MULTIPLIER) or DEFAULT_EXPECTED_QUEUE_MULTIPLIER),
        "upper_queue_multiplier": max(1.0, _number(multipliers.get("upper"), DEFAULT_UPPER_QUEUE_MULTIPLIER) or DEFAULT_UPPER_QUEUE_MULTIPLIER),
        "min_quote_lifetime_ms": max(0.0, _number(quoting.get("min_quote_lifetime_ms"), DEFAULT_MIN_QUOTE_LIFETIME_MS) or DEFAULT_MIN_QUOTE_LIFETIME_MS),
        "exploration_min_rest_ms": max(0.0, _number(exploration.get("minimum_rest_ms"), DEFAULT_EXPLORATION_MIN_REST_MS) or DEFAULT_EXPLORATION_MIN_REST_MS),
    }


def _read_ledger(path: Path, model_sha: str | None) -> tuple[list[dict[str, Any]], set[str], int]:
    rows: list[dict[str, Any]] = []
    shas: set[str] = set()
    invalid = 0
    try:
        handle = path.open("r", encoding="utf-8", errors="replace")
    except OSError:
        return rows, shas, invalid
    with handle:
        for line in handle:
            if not line.strip():
                continue
            try:
                row = json.loads(line)
            except json.JSONDecodeError:
                invalid += 1
                continue
            if not isinstance(row, dict) or str(row.get("strategy") or "") != STRATEGY:
                continue
            sha = str(row.get("model_sha") or "")
            if sha:
                shas.add(sha)
            if model_sha and sha != model_sha:
                continue
            rows.append(row)
    return rows, shas, invalid


def _read_tape(path: Path, tokens: set[str], start_ms: int | None, end_ms: int | None) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    trades: list[dict[str, Any]] = []
    rows = 0
    relevant = 0
    try:
        handle = path.open(newline="", encoding="utf-8", errors="replace")
    except OSError:
        return trades, {"present": False, "rows": 0, "relevant_rows": 0, "timestamp_resolution_ms": 1000}
    with handle:
        try:
            reader = csv.DictReader(handle)
            for row in reader:
                rows += 1
                token = str(row.get("asset_id") or "")
                if tokens and token not in tokens:
                    continue
                ts_ms = _integer(row.get("timestamp"), 0) * 1000
                if start_ms is not None and ts_ms < start_ms - 1000:
                    continue
                if end_ms is not None and ts_ms > end_ms + 1000:
                    continue
                side = str(row.get("side") or "").upper()
                price = _number(row.get("price"))
                size = _number(row.get("size"))
                received_ms = _integer(row.get("received_ms"), 0)
                if not token or ts_ms <= 0 or received_ms <= 0 or side not in {"BUY", "SELL"}:
                    continue
                if price is None or size is None or not (0.0 < price < 1.0) or size <= 0.0:
                    continue
                relevant += 1
                trades.append({
                    "token_id": token,
                    "exchange_ms": ts_ms,
                    "received_ms": received_ms,
                    "side": side,
                    "price": price,
                    "size": size,
                    "trade_id": str(row.get("transaction_hash") or row.get("trade_id") or ""),
                })
        except csv.Error:
            pass
    trades.sort(key=lambda row: (row["exchange_ms"], row["received_ms"], row["trade_id"]))
    return trades, {"present": True, "rows": rows, "relevant_rows": relevant, "timestamp_resolution_ms": 1000}


def _tick_size(order: dict[str, Any]) -> float:
    meta = _metadata(order)
    explicit = _number(meta.get("tick_size"))
    if explicit is not None and explicit > 0.0:
        return explicit
    for key in ("bid", "ask", "limit_price"):
        value = _number(order.get(key))
        if value is None:
            continue
        text = f"{value:.6f}".rstrip("0")
        if "." in text:
            decimals = len(text.split(".", 1)[1])
            if decimals:
                return 10.0 ** (-min(decimals, 4))
    return 0.01


def _distance_from_touch(order: dict[str, Any]) -> float | None:
    side = str(order.get("side") or "").upper()
    limit_price = _number(order.get("limit_price"))
    bid = _number(order.get("bid"))
    ask = _number(order.get("ask"))
    tick = _tick_size(order)
    if limit_price is None or tick <= 0.0:
        return None
    if side == "BUY" and bid is not None:
        return max(0.0, (bid - limit_price) / tick)
    if side == "SELL" and ask is not None:
        return max(0.0, (limit_price - ask) / tick)
    return None


def _queue_envelope(order: dict[str, Any], policy: dict[str, float]) -> tuple[float, float, float, float]:
    meta = _metadata(order)
    lower = _number(meta.get("queue_ahead_lower"), _number(order.get("queue_ahead"), 0.0)) or 0.0
    expected = _number(meta.get("queue_ahead_expected"))
    upper = _number(meta.get("queue_ahead_upper"))
    confidence = _number(meta.get("queue_confidence"), 0.5) or 0.5
    lower = max(0.0, lower)
    expected = max(lower, expected if expected is not None else lower * policy["expected_queue_multiplier"])
    upper = max(expected, upper if upper is not None else lower * policy["upper_queue_multiplier"])
    return lower, expected, upper, min(1.0, max(0.0, confidence))


def _trade_reaches(order: dict[str, Any], trade: dict[str, Any]) -> bool:
    side = str(order.get("side") or "").upper()
    limit_price = _number(order.get("limit_price"))
    if limit_price is None:
        return False
    if side == "BUY":
        return trade["side"] == "SELL" and trade["price"] <= limit_price + _EPS
    if side == "SELL":
        return trade["side"] == "BUY" and trade["price"] >= limit_price - _EPS
    return False


def _lifetime_bucket(value_ms: float) -> str:
    if value_ms < 250.0:
        return "<250ms"
    if value_ms < 1000.0:
        return "250ms-1s"
    if value_ms < 3000.0:
        return "1-3s"
    if value_ms < 5000.0:
        return "3-5s"
    return ">=5s"


def _classify(order: dict[str, Any]) -> str:
    if order["fills"] > 0:
        return "FILLED"
    if order["fill_opportunity_pessimistic"]:
        return "PESSIMISTIC_QUEUE_DEPLETED_BUT_NO_FILL"
    if order["fill_opportunity_expected"]:
        return "QUEUE_EXPECTED_DEPLETED_NOT_PESSIMISTIC"
    if order["fill_opportunity_lower"]:
        return "QUEUE_LOWER_DEPLETED_NOT_EXPECTED"
    if order["aggressive_volume"] > 0.0:
        return "AGGRESSIVE_FLOW_REACHED_PRICE_BUT_QUEUE_NOT_DEPLETED"
    distance = order.get("distance_from_touch_ticks")
    if distance is not None and distance >= 0.75:
        return "ORDER_NOT_AT_COMPETITIVE_LEVEL"
    return "NO_AGGRESSIVE_FLOW_REACHED_PRICE"


def _root_cause(funnel: dict[str, int], orders: list[dict[str, Any]]) -> tuple[str, str, str]:
    n = funnel["orders"]
    if n <= 0:
        return "NO_ORDERS", "INSUFFICIENT_EVIDENCE", "wait_for_orders"
    reach = funnel["trade_reachable"] / n
    pess = funnel["fill_opportunity_pessimistic"] / n
    cancelled_before_flow = sum(1 for row in orders if row["cancelled_before_first_flow"])
    if reach < 0.20:
        return "FLOW_OR_PLACEMENT", "NO", "market_selection_or_quote_placement"
    if funnel["fill_opportunity_pessimistic"] > 0 and funnel["full_fills"] + funnel["partial_fills"] == 0:
        return "PESSIMISTIC_OPPORTUNITY_WITHOUT_FILL", "INSUFFICIENT_EVIDENCE", "deterministic_exact_ws_replay"
    if pess < 0.05:
        return "QUEUE_COMPETITION_OR_LIFETIME", "NO", "single_dimension_lifetime_or_placement_challenger"
    if cancelled_before_flow / n > 0.50:
        return "LIFETIME_OR_CHURN", "NO", "ordinary_quote_lifetime_challenger"
    return "DATA_ACCUMULATION", "NO", "continue_bounded_paper_exploration"


def _aggregate(rows: Iterable[dict[str, Any]], key_name: str) -> list[dict[str, Any]]:
    buckets: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for row in rows:
        buckets[str(row.get(key_name) or "UNKNOWN")].append(row)
    out: list[dict[str, Any]] = []
    for key, items in buckets.items():
        count = len(items)
        out.append({
            key_name: key,
            "orders": count,
            "trade_reachable": sum(1 for row in items if row["trade_reachable"]),
            "lower_queue_depleted": sum(1 for row in items if row["queue_depleted_lower"]),
            "expected_queue_depleted": sum(1 for row in items if row["queue_depleted_expected"]),
            "pessimistic_queue_depleted": sum(1 for row in items if row["queue_depleted_pessimistic"]),
            "fill_opportunities": sum(1 for row in items if row["fill_opportunity_pessimistic"]),
            "filled_orders": sum(1 for row in items if row["fills"] > 0),
            "fills": sum(row["fills"] for row in items),
            "mean_rest_ms": sum(row["resting_time_ms"] for row in items) / count,
            "mean_near_miss_ratio": sum(row["near_miss_ratio"] for row in items) / count,
            "priority_resets": sum(1 for row in items if row["probable_priority_reset"]),
            "cancel_before_flow": sum(1 for row in items if row["cancelled_before_first_flow"]),
        })
    out.sort(key=lambda row: (-row["fill_opportunities"], -row["trade_reachable"], row[key_name]))
    return out


def summarize_maker_fillability(
    ledger_path: Path,
    trade_tape_path: Path,
    policy_path: Path | None = None,
    *,
    model_sha: str | None = None,
    now_ms: int | None = None,
) -> dict[str, Any]:
    """Build the conservative V7 maker fillability funnel for one exact SHA."""
    global _CACHE_KEY, _CACHE_VALUE
    ledger_path = Path(ledger_path)
    trade_tape_path = Path(trade_tape_path)
    policy_path = Path(policy_path) if policy_path is not None else None
    now_ms = int(time.time() * 1000) if now_ms is None else int(now_ms)
    key = (
        _stat_key(ledger_path), _stat_key(trade_tape_path),
        _stat_key(policy_path) if policy_path is not None else None,
        model_sha, now_ms // 5000,
    )
    with _CACHE_LOCK:
        if key == _CACHE_KEY and _CACHE_VALUE is not None:
            return _CACHE_VALUE

    policy_root, policy_hash = _policy(policy_path)
    policy = _policy_values(policy_root)
    rows, observed_shas, invalid_rows = _read_ledger(ledger_path, model_sha)
    order_rows = [row for row in rows if row.get("event_type") == "ORDER_SUBMITTED"]
    selected_sha = model_sha or (next(iter(observed_shas)) if len(observed_shas) == 1 else None)
    exact_sha_ok = bool(selected_sha) and len(selected_sha) == 40 and all(ch in "0123456789abcdef" for ch in selected_sha)

    order_map: dict[str, dict[str, Any]] = {}
    for row in order_rows:
        oid = str(row.get("order_id") or "")
        if not oid:
            continue
        order_map[oid] = {
            "raw": row,
            "order_id": oid,
            "market_id": str(row.get("market_id") or "UNKNOWN"),
            "event_id": str(row.get("event_id") or ""),
            "token_id": str(row.get("token_id") or ""),
            "side": str(row.get("side") or "UNKNOWN").upper(),
            "action": str(row.get("intended_action") or _metadata(row).get("action") or "UNKNOWN").upper(),
            "size": max(0.0, _number(row.get("intended_size"), 0.0) or 0.0),
            "limit_price": _number(row.get("limit_price")),
            "fills": 0,
            "filled_size": 0.0,
            "states": [],
        }

    for row in rows:
        oid = str(row.get("order_id") or "")
        order = order_map.get(oid)
        if order is None:
            continue
        event_type = str(row.get("event_type") or "")
        if event_type == "ORDER_STATE":
            state = str(row.get("order_state") or "UNKNOWN").upper()
            order["states"].append((state, _integer(row.get("recorded_ts_ms"), 0)))
        elif event_type == "FILL":
            order["fills"] += 1
            order["filled_size"] += max(0.0, _number(row.get("filled_size"), 0.0) or 0.0)

    tokens = {row["token_id"] for row in order_map.values() if row["token_id"]}
    exchange_starts = [_integer(row["raw"].get("exchange_ts_ms"), 0) for row in order_map.values()]
    start_ms = min((value for value in exchange_starts if value > 0), default=None)
    trades, tape_quality = _read_tape(trade_tape_path, tokens, start_ms, now_ms)
    by_token: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for trade in trades:
        by_token[trade["token_id"]].append(trade)

    completed: list[dict[str, Any]] = []
    last_terminal_by_lane: dict[tuple[str, str], dict[str, Any]] = {}
    for order in sorted(order_map.values(), key=lambda row: _integer(row["raw"].get("recorded_ts_ms"), 0)):
        raw = order.pop("raw")
        lower, expected, upper, confidence = _queue_envelope(raw, policy)
        decision_wall = _integer(raw.get("decision_ts_ms"), _integer(raw.get("receive_ts_ms"), _integer(raw.get("recorded_ts_ms"), now_ms)))
        effective_wall = decision_wall + int(math.ceil(policy["submission_latency_ms"]))
        effective_exchange = _integer(raw.get("exchange_ts_ms"), 0) + int(math.ceil(policy["submission_latency_ms"]))
        cancel_pending = next((ts for state, ts in order["states"] if state == "CANCEL_PENDING" and ts > 0), 0)
        cancelled = next((ts for state, ts in reversed(order["states"]) if state == "CANCELLED" and ts > 0), 0)
        fill_terminal = next((ts for state, ts in reversed(order["states"]) if state == "FILLED" and ts > 0), 0)
        end_wall = cancelled or fill_terminal or now_ms
        if end_wall < effective_wall:
            end_wall = effective_wall
        rest_ms = float(max(0, end_wall - effective_wall))

        aggressive_volume = 0.0
        volume_at = 0.0
        volume_through = 0.0
        prints = 0
        first_flow_ms = 0
        for trade in by_token.get(order["token_id"], []):
            if trade["exchange_ms"] <= effective_exchange:
                continue
            if trade["received_ms"] <= effective_wall:
                continue
            if trade["exchange_ms"] > end_wall:
                break
            if not _trade_reaches(order, trade):
                continue
            prints += 1
            aggressive_volume += trade["size"]
            first_flow_ms = first_flow_ms or trade["exchange_ms"]
            if order["limit_price"] is not None and abs(trade["price"] - order["limit_price"]) <= 5e-7:
                volume_at += trade["size"]
            else:
                volume_through += trade["size"]

        distance = _distance_from_touch(raw)
        queue_depleted_lower = aggressive_volume + _EPS >= lower
        queue_depleted_expected = aggressive_volume + _EPS >= expected
        queue_depleted_pessimistic = aggressive_volume + _EPS >= upper
        fill_opp_lower = aggressive_volume > lower + _EPS
        fill_opp_expected = aggressive_volume > expected + _EPS
        fill_opp_pess = aggressive_volume > upper + _EPS
        near_miss = aggressive_volume / upper if upper > _EPS else (1.0 if aggressive_volume > 0.0 else 0.0)
        lane = (order["token_id"], order["side"])
        previous = last_terminal_by_lane.get(lane)
        probable_reset = False
        if previous is not None:
            gap = effective_wall - previous["end_wall_ms"]
            probable_reset = 0 <= gap <= 1000 and (
                previous.get("limit_price") != order["limit_price"] or previous.get("action") != order["action"]
            )

        result = {
            **order,
            "policy_hash": policy_hash,
            "decision_wall_ms": decision_wall,
            "effective_wall_ms": effective_wall,
            "effective_exchange_ms": effective_exchange,
            "end_wall_ms": end_wall,
            "cancel_request_ms": cancel_pending,
            "cancel_effective_ms": cancelled,
            "resting_time_ms": rest_ms,
            "lifetime_bucket": _lifetime_bucket(rest_ms),
            "distance_from_touch_ticks": distance,
            "queue_ahead_lower": lower,
            "queue_ahead_expected": expected,
            "queue_ahead_upper": upper,
            "queue_confidence": confidence,
            "prints_at_or_through_price": prints,
            "aggressive_volume_at_price": volume_at,
            "aggressive_volume_through_price": volume_through,
            "aggressive_volume": aggressive_volume,
            "first_flow_exchange_ms": first_flow_ms,
            "trade_reachable": aggressive_volume > 0.0,
            "queue_depleted_lower": queue_depleted_lower,
            "queue_depleted_expected": queue_depleted_expected,
            "queue_depleted_pessimistic": queue_depleted_pessimistic,
            "fill_opportunity_lower": fill_opp_lower,
            "fill_opportunity_expected": fill_opp_expected,
            "fill_opportunity_pessimistic": fill_opp_pess,
            "counterfactual_fill_lower": max(0.0, min(order["size"], aggressive_volume - lower)),
            "counterfactual_fill_expected": max(0.0, min(order["size"], aggressive_volume - expected)),
            "counterfactual_fill_pessimistic": max(0.0, min(order["size"], aggressive_volume - upper)),
            "near_miss_ratio": near_miss,
            "cancelled_before_first_flow": bool(cancelled and aggressive_volume <= 0.0),
            "cancelled_below_min_quote_lifetime": bool(cancelled and rest_ms + _EPS < policy["min_quote_lifetime_ms"]),
            "probable_priority_reset": probable_reset,
            "diagnostic_source": "canonical_ledger_plus_taker_rest_tape",
            "tape_timestamp_resolution_ms": tape_quality["timestamp_resolution_ms"],
        }
        result["fillability_classification"] = _classify(result)
        completed.append(result)
        last_terminal_by_lane[lane] = result

    classifications = Counter(row["fillability_classification"] for row in completed if row["fills"] == 0)
    funnel = {
        "orders": len(completed),
        "orders_effective": len(completed),
        "orders_rested": len(completed),
        "trade_reachable": sum(1 for row in completed if row["trade_reachable"]),
        "lower_queue_depleted": sum(1 for row in completed if row["queue_depleted_lower"]),
        "expected_queue_depleted": sum(1 for row in completed if row["queue_depleted_expected"]),
        "pessimistic_queue_depleted": sum(1 for row in completed if row["queue_depleted_pessimistic"]),
        "fill_opportunity_lower": sum(1 for row in completed if row["fill_opportunity_lower"]),
        "fill_opportunity_expected": sum(1 for row in completed if row["fill_opportunity_expected"]),
        "fill_opportunity_pessimistic": sum(1 for row in completed if row["fill_opportunity_pessimistic"]),
        "partial_fills": sum(1 for row in completed if row["fills"] > 0 and row["filled_size"] + _EPS < row["size"]),
        "full_fills": sum(1 for row in completed if row["fills"] > 0 and row["filled_size"] + _EPS >= row["size"]),
        "cancelled_before_flow": sum(1 for row in completed if row["cancelled_before_first_flow"]),
        "priority_resets": sum(1 for row in completed if row["probable_priority_reset"]),
    }
    root_cause, simulator_bug, next_experiment = _root_cause(funnel, completed)
    market_rows = _aggregate(completed, "market_id")
    action_rows = _aggregate(completed, "action")
    lifetime_rows = _aggregate(completed, "lifetime_bucket")
    near_misses = sorted(
        (row for row in completed if row["fills"] == 0),
        key=lambda row: (-row["near_miss_ratio"], -row["aggressive_volume"], row["order_id"]),
    )[:20]

    result = {
        "schema": "polymarket_v7_maker_fillability_v1",
        "generated_ts_ms": now_ms,
        "paper_only": True,
        "authenticated_execution": False,
        "real_order_submission": False,
        "strategy": STRATEGY,
        "runtime_sha": selected_sha,
        "exact_sha_ok": exact_sha_ok,
        "observed_maker_shas": sorted(observed_shas),
        "policy_hash": policy_hash,
        "policy": policy,
        "ledger_invalid_rows": invalid_rows,
        "funnel": funnel,
        "zero_fill_reasons": dict(sorted(classifications.items())),
        "root_cause": root_cause,
        "simulator_bug_suspected": simulator_bug,
        "next_experiment": next_experiment,
        "markets": market_rows,
        "actions": action_rows,
        "lifetimes": lifetime_rows,
        "near_misses": [{key: value for key, value in row.items() if key != "states"} for row in near_misses],
        "orders": completed,
        "quality": {
            **tape_quality,
            "trade_tape_role": "independent_coarse_fillability_diagnostic_not_simulator_truth",
            "same_second_exchange_events_rejected": True,
            "taker_only_side_semantics_required": True,
            "queue_envelope_from_order_metadata_or_frozen_policy": True,
            "simulator_relaxation_performed": False,
        },
    }
    with _CACHE_LOCK:
        _CACHE_KEY = key
        _CACHE_VALUE = result
    return result
