#!/usr/bin/env python3
"""Prospective Maker anchor eligibility using arrival-time evidence only.

This module never inspects future prints, markouts, fills, or settlement. It is
safe to use before a confirmatory Maker anchor is frozen.
"""
from __future__ import annotations

import math
import time
from typing import Any


def finite(value: Any) -> float | None:
    if value is None or isinstance(value, bool):
        return None
    try:
        out = float(value)
    except (TypeError, ValueError, OverflowError):
        return None
    return out if math.isfinite(out) else None


def evaluate_anchor(order: dict[str, Any], book: Any, status: dict[str, Any], protocol: dict[str, Any], *, now_ms: int | None = None) -> dict[str, Any]:
    """Return ELIGIBLE, PENDING, or INELIGIBLE without using future outcomes."""
    now_ms = int(time.time() * 1000) if now_ms is None else int(now_ms)
    maker = protocol.get("maker") if isinstance(protocol.get("maker"), dict) else {}
    maximum_age = int(maker.get("maximum_feature_age_ms") or 0)
    metadata = order.get("metadata") if isinstance(order.get("metadata"), dict) else {}
    market = str(order.get("market_id") or "")
    token = str(order.get("token_id") or "")
    receive_ms = int(order.get("receive_ts_ms") or 0)
    arrival_mono = int(metadata.get("arrival_receive_monotonic_ns") or 0)
    arrival_exchange = int(metadata.get("arrival_exchange_event_ns") or 0)
    if not market or not token or receive_ms <= 0 or arrival_mono <= 0 or arrival_exchange <= 0:
        return {"state": "INELIGIBLE", "reason": "MISSING_NATIVE_ARRIVAL_CLOCK_OR_IDENTITY"}
    if maximum_age <= 0:
        return {"state": "INELIGIBLE", "reason": "INVALID_FROZEN_MAXIMUM_FEATURE_AGE"}

    if (
        status.get("state") != "running"
        or status.get("paper_only") is not True
        or status.get("authenticated_execution") is not False
        or status.get("real_order_submission") is not False
        or status.get("evidence_complete") is not True
        or status.get("model_sha") != getattr(book, "model_sha", None)
    ):
        return {"state": "PENDING", "reason": "BOOK_STATUS_NOT_CAUSALLY_READY"}
    status_ms = int(status.get("timestamp_ms") or 0)
    if status_ms <= 0 or status_ms > now_ms or now_ms - status_ms > 2000:
        return {"state": "PENDING", "reason": "BOOK_STATUS_STALE_OR_FUTURE"}
    if (
        str(status.get("observer_session_id") or "") != str(getattr(book, "session", ""))
        or int(status.get("connection_epoch") or 0) != int(getattr(book, "epoch", 0))
    ):
        return {"state": "PENDING", "reason": "BOOK_SESSION_NOT_CAUSALLY_ALIGNED"}
    if (
        int(getattr(book, "watermark_monotonic_ns", 0)) < arrival_mono
        or int(status.get("book_watermark_receive_monotonic_ns") or 0) < arrival_mono
    ):
        return {"state": "PENDING", "reason": "BOOK_WATERMARK_BEFORE_ARRIVAL"}

    history = list(getattr(book, "history", {}).get((market, token), []))
    origin = next((
        row for row in reversed(history)
        if int(row.get("receive_monotonic_ns") or 0) <= arrival_mono
        and int(row.get("receive_wall_ms") or 0) <= receive_ms
    ), None)
    if origin is None:
        return {"state": "PENDING", "reason": "ARRIVAL_BOOK_NOT_YET_AVAILABLE"}
    if origin.get("valid") is not True or origin.get("lineage_continuous") is not True:
        return {"state": "INELIGIBLE", "reason": "INVALID_ARRIVAL_BOOK"}
    if origin.get("features_valid") is not True:
        return {"state": "INELIGIBLE", "reason": "ARRIVAL_FEATURES_INVALID"}
    origin_receive = int(origin.get("receive_wall_ms") or 0)
    if origin_receive <= 0 or receive_ms < origin_receive or receive_ms - origin_receive > maximum_age:
        return {"state": "INELIGIBLE", "reason": "STALE_ARRIVAL_FEATURES"}
    try:
        bid = float(origin["best_bid"])
        ask = float(origin["best_ask"])
        tick = float(origin["tick_size"])
    except (KeyError, TypeError, ValueError, OverflowError):
        return {"state": "INELIGIBLE", "reason": "ARRIVAL_L1_MISSING"}
    if not (0.0 < bid < ask < 1.0 and 0.0 < tick < 1.0):
        return {"state": "INELIGIBLE", "reason": "ARRIVAL_L1_INVALID"}
    return {
        "state": "ELIGIBLE",
        "reason": "PROSPECTIVE_ARRIVAL_EVIDENCE_VALID",
        "origin": origin,
        "origin_receive_wall_ms": origin_receive,
        "feature_age_ms": receive_ms - origin_receive,
        "observer_session_id": str(getattr(book, "session", "")),
        "connection_epoch": int(getattr(book, "epoch", 0)),
        "book_gap_counter": int(getattr(book, "gaps", 0)),
    }
