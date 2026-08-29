#!/usr/bin/env python3
from __future__ import annotations

import csv
import json
import os
import time
from pathlib import Path
from typing import Any, Callable


DEFAULT_CANCEL_LATENCY_MS = 100
DEFAULT_TAPE_GRACE_MS = 30_000


class CancelAwareOrders(dict[str, dict[str, Any]]):
    """Intercept only economic cancellation deletes; full fills still delete."""

    def __init__(self, *args: Any, on_cancel: Callable[[str, dict[str, Any]], bool], **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        self._on_cancel = on_cancel

    def __delitem__(self, key: str) -> None:
        row = self.get(key)
        if isinstance(row, dict) and self._on_cancel(str(key), row):
            return
        super().__delitem__(key)


def request_cancel(order: dict[str, Any], *, processing_ms: int, latency_ms: int, grace_ms: int, reason: str) -> None:
    if str(order.get("order_state") or "OPEN") == "CANCEL_PENDING":
        return
    request = int(processing_ms)
    latency = max(0, int(latency_ms))
    grace = max(0, int(grace_ms))
    order["order_state"] = "CANCEL_PENDING"
    order["cancel_reason"] = str(reason)
    order["cancel_requested_received_ms"] = request
    order["cancel_effective_event_ms"] = request + latency
    order["cancel_effective_received_ms"] = request + latency
    order["cancel_finalize_received_ms"] = request + latency + grace


def live_until_event_ms(order: dict[str, Any], ttl_seconds: int) -> int:
    pending = int(float(order.get("cancel_effective_event_ms") or 0))
    if str(order.get("order_state") or "OPEN") == "CANCEL_PENDING" and pending > 0:
        return pending
    arrival = int(float(order.get("created_event_ms") or 0))
    return arrival + max(0, int(ttl_seconds)) * 1000


def causal_fill_eligible(row: dict[str, str], order: dict[str, Any], *, processing_ms: int, ttl_seconds: int) -> bool:
    try:
        event_ms = int(float(row.get("timestamp") or 0.0) * 1000)
        received_ms = int(float(row.get("received_ms") or 0.0))
        arrival_event_ms = int(float(order.get("created_event_ms") or 0.0))
        arrival_received_ms = int(float(order.get("created_received_ms") or 0.0))
    except (TypeError, ValueError, OverflowError):
        return False
    if event_ms <= arrival_event_ms or received_ms <= arrival_received_ms:
        return False
    if received_ms <= 0 or received_ms > int(processing_ms):
        return False
    return event_ms <= live_until_event_ms(order, ttl_seconds)


def finalize_due_cancels(state_path: Path, *, processing_ms: int) -> list[dict[str, Any]]:
    """Remove only after cancel latency plus bounded delayed-tape grace."""
    try:
        state = json.loads(state_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return []
    if not isinstance(state, dict):
        return []
    orders = state.get("orders") if isinstance(state.get("orders"), dict) else {}
    finalized: list[dict[str, Any]] = []
    for market_id, order in list(orders.items()):
        if not isinstance(order, dict) or str(order.get("order_state") or "") != "CANCEL_PENDING":
            continue
        try:
            deadline = int(float(order.get("cancel_finalize_received_ms") or 0))
        except (TypeError, ValueError, OverflowError):
            deadline = 0
        if deadline > 0 and int(processing_ms) >= deadline:
            finalized.append({**order, "market_id": market_id})
            del orders[market_id]
    if finalized:
        state["orders"] = orders
        tmp = state_path.with_name(state_path.name + f".tmp.{os.getpid()}")
        tmp.write_text(json.dumps(state, indent=2, sort_keys=True) + "\n", encoding="utf-8")
        os.replace(tmp, state_path)
    return finalized


def append_final_cancel_log(base: Any, run_dir: Path, rows: list[dict[str, Any]], *, timestamp: int) -> None:
    fields = ["timestamp", "action", "market_id", "slug", "side", "token_id", "limit_price", "remaining_shares", "queue_ahead", "signal_edge", "confidence", "fill_probability", "expected_value", "toxicity_score", "flow_rate", "fee_source"]
    for row in rows:
        base.append_csv(run_dir / "maker_order_log.csv", fields, {**row, "timestamp": timestamp, "action": "CANCEL_EFFECTIVE"})


def annotate_contract(run_dir: Path, *, latency_ms: int, grace_ms: int) -> None:
    for name in ("state.json", "status.json"):
        path = run_dir / name
        try:
            value = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        if not isinstance(value, dict):
            continue
        orders = value.get("orders") if isinstance(value.get("orders"), dict) else {}
        value["maker_cancel_contract"] = "open_to_cancel_pending_to_cancelled_with_event_time_fill_until_effective_cancel"
        value["cancel_latency_ms"] = int(latency_ms)
        value["cancel_tape_grace_ms"] = int(grace_ms)
        value["cancel_pending_orders"] = sum(
            isinstance(row, dict) and str(row.get("order_state") or "") == "CANCEL_PENDING"
            for row in orders.values()
        )
        tmp = path.with_name(path.name + f".tmp.{os.getpid()}")
        tmp.write_text(json.dumps(value, indent=2, sort_keys=True) + "\n", encoding="utf-8")
        os.replace(tmp, path)
