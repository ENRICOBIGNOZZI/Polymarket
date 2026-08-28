#!/usr/bin/env python3
"""Forward exact-WS replay for V7 maker fillability diagnostics.

This module deliberately complements, rather than rewrites, the historical
REST-tape bootstrap. Exact-WS observations are collected by a separate
read-only observer and are only used for orders whose complete observed life is
inside the exact evidence window. Public trade quantity is conserved separately
under lower/expected/pessimistic queue scenarios, mirroring the PAPER queue
engine's one-print/one-volume contract.
"""
from __future__ import annotations

import json
import math
from collections import Counter, defaultdict
from pathlib import Path
from typing import Any

import v7_maker_fillability as coarse

_EPS = 1e-12


def _number(value: Any, default: float | None = None) -> float | None:
    try:
        out = float(value)
    except (TypeError, ValueError, OverflowError):
        return default
    return out if math.isfinite(out) else default


def _read_status(path: Path, model_sha: str | None) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {"present": False, "evidence_complete": False}
    if not isinstance(value, dict):
        return {"present": False, "evidence_complete": False}
    same_sha = not model_sha or str(value.get("model_sha") or "") == model_sha
    return {
        "present": True,
        "same_sha": same_sha,
        "evidence_complete": bool(value.get("evidence_complete")) and same_sha,
        "dropped_events": int(_number(value.get("dropped_events"), 0) or 0),
        "decoder_failures": int(_number(value.get("decoder_failures"), 0) or 0),
        "reconnects": int(_number(value.get("reconnects"), 0) or 0),
        "connection_epoch": int(_number(value.get("connection_epoch"), 0) or 0),
        "last_exchange_event_ns": int(_number(value.get("last_exchange_event_ns"), 0) or 0),
        "last_receive_wall_ms": int(_number(value.get("last_receive_wall_ms"), 0) or 0),
        "state": str(value.get("state") or "UNKNOWN"),
    }


def _read_exact(path: Path, model_sha: str | None) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    seen: set[tuple[Any, ...]] = set()
    try:
        handle = path.open("r", encoding="utf-8", errors="replace")
    except OSError:
        return rows
    with handle:
        for line in handle:
            if not line.strip():
                continue
            try:
                row = json.loads(line)
            except json.JSONDecodeError:
                continue
            if not isinstance(row, dict):
                continue
            if model_sha and str(row.get("model_sha") or "") != model_sha:
                continue
            token = str(row.get("token_id") or "")
            side = str(row.get("aggressor_side") or "").upper()
            price = _number(row.get("price"))
            size = _number(row.get("size"))
            exchange_ns = int(_number(row.get("exchange_event_ns"), 0) or 0)
            receive_wall_ms = int(_number(row.get("receive_wall_ms"), 0) or 0)
            receive_mono_ns = int(_number(row.get("receive_monotonic_ns"), 0) or 0)
            epoch = int(_number(row.get("connection_epoch"), 0) or 0)
            state_version = int(_number(row.get("state_version"), 0) or 0)
            if not token or side not in {"BUY", "SELL"} or price is None or size is None:
                continue
            if exchange_ns <= 0 or receive_wall_ms <= 0 or receive_mono_ns <= 0 or size <= 0.0:
                continue
            if not bool(row.get("lineage_continuous")):
                continue
            key = (epoch, token, state_version, exchange_ns, side, round(price, 8), round(size, 8))
            if key in seen:
                continue
            seen.add(key)
            rows.append({
                "token_id": token,
                "aggressor_side": side,
                "price": price,
                "size": size,
                "exchange_event_ns": exchange_ns,
                "receive_wall_ms": receive_wall_ms,
                "receive_monotonic_ns": receive_mono_ns,
                "connection_epoch": epoch,
                "state_version": state_version,
            })
    rows.sort(key=lambda row: (
        row["exchange_event_ns"], row["receive_monotonic_ns"], row["connection_epoch"],
        row["token_id"], row["state_version"],
    ))
    return rows


def _compatible(order: dict[str, Any], trade: dict[str, Any]) -> bool:
    side = str(order.get("side") or "").upper()
    price = _number(order.get("limit_price"))
    if price is None:
        return False
    if side == "BUY":
        return trade["aggressor_side"] == "SELL" and trade["price"] <= price + _EPS
    if side == "SELL":
        return trade["aggressor_side"] == "BUY" and trade["price"] >= price - _EPS
    return False


def _covered(order: dict[str, Any], first_wall_ms: int, last_wall_ms: int) -> bool:
    start = int(_number(order.get("effective_wall_ms"), 0) or 0)
    end = int(_number(order.get("end_wall_ms"), 0) or 0)
    return start >= first_wall_ms and end > start and end <= last_wall_ms


def _aggregate(rows: list[dict[str, Any]], key_name: str) -> list[dict[str, Any]]:
    buckets: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for row in rows:
        buckets[str(row.get(key_name) or "UNKNOWN")].append(row)
    out: list[dict[str, Any]] = []
    for key, items in buckets.items():
        n = len(items)
        out.append({
            key_name: key,
            "orders": n,
            "trade_reachable": sum(1 for row in items if row["trade_reachable"]),
            "lower_queue_depleted": sum(1 for row in items if row["queue_depleted_lower"]),
            "expected_queue_depleted": sum(1 for row in items if row["queue_depleted_expected"]),
            "pessimistic_queue_depleted": sum(1 for row in items if row["queue_depleted_pessimistic"]),
            "fill_opportunities": sum(1 for row in items if row["fill_opportunity_pessimistic"]),
            "filled_orders": sum(1 for row in items if int(row.get("fills") or 0) > 0),
            "mean_rest_ms": sum(float(row.get("resting_time_ms") or 0.0) for row in items) / n,
            "mean_near_miss_ratio": sum(float(row.get("near_miss_ratio") or 0.0) for row in items) / n,
            "cancelled_before_observed_future_flow": sum(1 for row in items if row["cancelled_before_observed_future_flow"]),
        })
    out.sort(key=lambda row: (-row["fill_opportunities"], -row["trade_reachable"], row[key_name]))
    return out


def replay_exact_ws(
    coarse_report: dict[str, Any],
    exact_rows: list[dict[str, Any]],
    *,
    evidence_complete: bool,
    future_flow_lookahead_ms: int = 5000,
) -> dict[str, Any]:
    """Replay exact WS trades with per-print conservation across own orders."""
    if not exact_rows:
        return {"present": False, "evidence_complete": False, "orders": [], "funnel": {}}
    first_wall = min(row["receive_wall_ms"] for row in exact_rows)
    last_wall = max(row["receive_wall_ms"] for row in exact_rows)
    source_orders = [dict(row) for row in coarse_report.get("orders") or [] if isinstance(row, dict)]
    orders = [row for row in source_orders if _covered(row, first_wall, last_wall)]
    orders.sort(key=lambda row: (int(row.get("effective_wall_ms") or 0), str(row.get("order_id") or "")))

    scenarios = {
        "lower": "queue_ahead_lower",
        "expected": "queue_ahead_expected",
        "pessimistic": "queue_ahead_upper",
    }
    state: dict[str, dict[str, dict[str, float]]] = {}
    for scenario, qkey in scenarios.items():
        state[scenario] = {}
        for order in orders:
            oid = str(order.get("order_id") or "")
            state[scenario][oid] = {
                "queue_initial": max(0.0, float(order.get(qkey) or 0.0)),
                "queue_remaining": max(0.0, float(order.get(qkey) or 0.0)),
                "own_remaining": max(0.0, float(order.get("size") or 0.0)),
                "counterfactual_fill": 0.0,
                "conserved_consumed": 0.0,
            }
    for order in orders:
        order["exact_raw_aggressive_volume"] = 0.0
        order["exact_prints_at_or_through"] = 0
        order["first_exact_flow_wall_ms"] = 0

    for trade in exact_rows:
        eligible: list[dict[str, Any]] = []
        for order in orders:
            if str(order.get("token_id") or "") != trade["token_id"]:
                continue
            start_wall = int(order.get("effective_wall_ms") or 0)
            end_wall = int(order.get("end_wall_ms") or 0)
            effective_exchange_ms = int(order.get("effective_exchange_ms") or 0)
            if trade["receive_wall_ms"] <= start_wall or trade["receive_wall_ms"] > end_wall:
                continue
            if effective_exchange_ms > 0 and trade["exchange_event_ns"] <= effective_exchange_ms * 1_000_000:
                continue
            if not _compatible(order, trade):
                continue
            eligible.append(order)
            order["exact_raw_aggressive_volume"] += trade["size"]
            order["exact_prints_at_or_through"] += 1
            if not order["first_exact_flow_wall_ms"]:
                order["first_exact_flow_wall_ms"] = trade["receive_wall_ms"]
        if not eligible:
            continue
        eligible.sort(key=lambda row: (int(row.get("effective_wall_ms") or 0), str(row.get("order_id") or "")))
        for scenario in scenarios:
            available = float(trade["size"])
            for order in eligible:
                if available <= _EPS:
                    break
                oid = str(order.get("order_id") or "")
                slot = state[scenario][oid]
                queue_used = min(slot["queue_remaining"], available)
                slot["queue_remaining"] -= queue_used
                slot["conserved_consumed"] += queue_used
                available -= queue_used
                if available <= _EPS or slot["own_remaining"] <= _EPS:
                    continue
                own_fill = min(slot["own_remaining"], available)
                slot["own_remaining"] -= own_fill
                slot["counterfactual_fill"] += own_fill
                slot["conserved_consumed"] += own_fill
                available -= own_fill

    for order in orders:
        oid = str(order.get("order_id") or "")
        for scenario in scenarios:
            slot = state[scenario][oid]
            order[f"queue_remaining_{scenario}"] = slot["queue_remaining"]
            order[f"counterfactual_fill_{scenario}"] = slot["counterfactual_fill"]
            order[f"conserved_consumed_{scenario}"] = slot["conserved_consumed"]
            order[f"queue_depleted_{scenario}"] = slot["queue_remaining"] <= _EPS
            order[f"fill_opportunity_{scenario}"] = slot["counterfactual_fill"] > _EPS
        upper = state["pessimistic"][oid]
        q0 = upper["queue_initial"]
        if q0 > _EPS:
            depleted = max(0.0, q0 - upper["queue_remaining"])
            order["near_miss_ratio"] = depleted / q0
        else:
            order["near_miss_ratio"] = 1.0 if order["fill_opportunity_pessimistic"] else 0.0
        order["trade_reachable"] = bool(order["exact_raw_aggressive_volume"] > _EPS)
        cancelled = int(order.get("cancel_effective_ms") or 0)
        observed_future = False
        if cancelled > 0 and not order["trade_reachable"]:
            horizon = cancelled + max(0, future_flow_lookahead_ms)
            for trade in exact_rows:
                if trade["receive_wall_ms"] <= cancelled or trade["receive_wall_ms"] > horizon:
                    continue
                if str(order.get("token_id") or "") != trade["token_id"] or not _compatible(order, trade):
                    continue
                observed_future = True
                break
        order["cancelled_before_observed_future_flow"] = observed_future
        if int(order.get("fills") or 0) > 0:
            classification = "FILLED"
        elif order["fill_opportunity_pessimistic"]:
            classification = "PESSIMISTIC_QUEUE_DEPLETED_BUT_NO_FILL"
        elif order["fill_opportunity_expected"]:
            classification = "QUEUE_EXPECTED_DEPLETED_NOT_PESSIMISTIC"
        elif order["fill_opportunity_lower"]:
            classification = "QUEUE_LOWER_DEPLETED_NOT_EXPECTED"
        elif order["trade_reachable"]:
            classification = "AGGRESSIVE_FLOW_REACHED_PRICE_BUT_QUEUE_NOT_DEPLETED"
        elif observed_future:
            classification = "CANCELLED_BEFORE_OBSERVED_FUTURE_FLOW"
        else:
            classification = "NO_AGGRESSIVE_FLOW_REACHED_PRICE"
        order["fillability_classification"] = classification
        order["diagnostic_source"] = "independent_exact_public_ws"

    reasons = Counter(row["fillability_classification"] for row in orders if int(row.get("fills") or 0) == 0)
    funnel = {
        "orders": len(orders),
        "orders_effective": len(orders),
        "orders_rested": len(orders),
        "trade_reachable": sum(1 for row in orders if row["trade_reachable"]),
        "lower_queue_depleted": sum(1 for row in orders if row["queue_depleted_lower"]),
        "expected_queue_depleted": sum(1 for row in orders if row["queue_depleted_expected"]),
        "pessimistic_queue_depleted": sum(1 for row in orders if row["queue_depleted_pessimistic"]),
        "fill_opportunity_lower": sum(1 for row in orders if row["fill_opportunity_lower"]),
        "fill_opportunity_expected": sum(1 for row in orders if row["fill_opportunity_expected"]),
        "fill_opportunity_pessimistic": sum(1 for row in orders if row["fill_opportunity_pessimistic"]),
        "partial_fills": sum(1 for row in orders if int(row.get("fills") or 0) > 0 and float(row.get("filled_size") or 0) + _EPS < float(row.get("size") or 0)),
        "full_fills": sum(1 for row in orders if int(row.get("fills") or 0) > 0 and float(row.get("filled_size") or 0) + _EPS >= float(row.get("size") or 0)),
        "cancelled_before_observed_future_flow": sum(1 for row in orders if row["cancelled_before_observed_future_flow"]),
    }
    if not evidence_complete:
        bug = "INSUFFICIENT_EVIDENCE"
    elif funnel["fill_opportunity_pessimistic"] > 0 and funnel["partial_fills"] + funnel["full_fills"] == 0:
        # This observer uses an independent WS connection, so even complete
        # observer evidence cannot prove the execution runtime saw the same
        # lineage. Escalate to same-feed deterministic replay, never relax fills.
        bug = "SAME_FEED_REPLAY_REQUIRED"
    else:
        bug = "NO"
    if funnel["orders"] == 0:
        root = "EXACT_WS_DATA_ACCUMULATION"
        next_experiment = "continue_exact_ws_collection"
    elif funnel["trade_reachable"] / funnel["orders"] < 0.20:
        root = "FLOW_OR_PLACEMENT"
        next_experiment = "market_selection_or_quote_placement"
    elif funnel["fill_opportunity_pessimistic"] == 0:
        root = "QUEUE_COMPETITION_OR_LIFETIME"
        next_experiment = "single_dimension_lifetime_or_placement_challenger"
    elif bug == "SAME_FEED_REPLAY_REQUIRED":
        root = "PESSIMISTIC_OPPORTUNITY_WITHOUT_RECORDED_FILL"
        next_experiment = "deterministic_same_feed_replay"
    else:
        root = "DATA_ACCUMULATION"
        next_experiment = "continue_bounded_paper_exploration"
    return {
        "present": True,
        "evidence_complete": evidence_complete,
        "coverage_start_wall_ms": first_wall,
        "coverage_end_wall_ms": last_wall,
        "orders": orders,
        "funnel": funnel,
        "zero_fill_reasons": dict(sorted(reasons.items())),
        "root_cause": root,
        "simulator_bug_suspected": bug,
        "next_experiment": next_experiment,
        "actions": _aggregate(orders, "action"),
        "markets": _aggregate(orders, "market_id"),
        "lifetimes": _aggregate(orders, "lifetime_bucket"),
        "near_misses": sorted(
            (row for row in orders if int(row.get("fills") or 0) == 0),
            key=lambda row: (-float(row.get("near_miss_ratio") or 0.0), str(row.get("order_id") or "")),
        )[:20],
        "quality": {
            "source": "independent_exact_public_ws",
            "per_print_conservation": True,
            "same_feed_as_execution_runtime": False,
            "simulator_relaxation_performed": False,
        },
    }


def summarize_best_available_fillability(
    ledger_path: Path,
    trade_tape_path: Path,
    policy_path: Path | None = None,
    *,
    model_sha: str | None = None,
    now_ms: int | None = None,
) -> dict[str, Any]:
    report = coarse.summarize_maker_fillability(
        ledger_path, trade_tape_path, policy_path, model_sha=model_sha, now_ms=now_ms)
    run_root = Path(ledger_path).resolve().parent.parent
    exact_path = run_root / "micro_maker" / "fillability_ws.jsonl"
    status_path = run_root / "micro_maker" / "fillability_ws_status.json"
    status = _read_status(status_path, model_sha)
    rows = _read_exact(exact_path, model_sha)
    exact = replay_exact_ws(report, rows, evidence_complete=bool(status.get("evidence_complete")))
    exact["observer_status"] = status
    report["forward_exact_ws"] = exact
    report["quality"]["exact_ws_present"] = bool(exact.get("present"))
    report["quality"]["exact_ws_evidence_complete"] = bool(exact.get("evidence_complete"))
    return report
