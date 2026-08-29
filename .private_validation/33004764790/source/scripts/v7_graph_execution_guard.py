#!/usr/bin/env python3
from __future__ import annotations

"""Neutral execution-state helpers for the canonical Graph/RV v3 guard.

This module is intentionally *not* an economic admission guard.  It contains
only reusable causal fill simulation, state-transport dominance and
chronological dependence helpers used by ``v7_graph_roundtrip_guard``.

There is exactly one Graph/RV economic evidence contract in V7: fixed-horizon,
depth-aware executable round-trip liquidation in
``v7_graph_roundtrip_guard.py``.  No terminal-settlement or quoted-edge PnL
path exists here.
"""

import math
import random
import statistics
from typing import Any

import v7_graph_forward_guard as base

HELPER_SCHEMA = "v7_graph_execution_state_helpers_v3"


def _simulate_fills(
    session: dict[str, Any], tape: list[dict[str, Any]]
) -> tuple[list[dict[str, Any]], list[float], int, int]:
    """Replay same-window public flow using both receive and market-event clocks."""
    legs = [dict(leg) for leg in session.get("legs", []) if isinstance(leg, dict)]
    queues = [max(0.0, float(base.finite(leg.get("queue_ahead"), 0.0))) for leg in legs]
    remaining = [max(0.0, float(base.finite(leg.get("target_shares"), 0.0))) for leg in legs]
    filled = [0.0] * len(legs)

    origin_received = int(session.get("origin_received_ms") or 0)
    deadline_received = int(session.get("deadline_received_ms") or 0)
    origin_event = int(session.get("origin_event_ms") or 0)
    deadline_event = int(session.get("deadline_event_ms") or 0)
    if not legs or min(origin_received, deadline_received, origin_event, deadline_event) <= 0:
        return legs, filled, 0, (1 << len(legs)) - 1

    for trade in tape:
        received_ms = int(trade.get("received_ms") or 0)
        event_ms = int(trade.get("event_ms") or 0)
        if not (origin_received < received_ms <= deadline_received):
            continue
        if not (origin_event < event_ms <= deadline_event):
            continue
        if str(trade.get("side") or "").upper() != "SELL":
            continue
        capacity = max(0.0, float(base.finite(trade.get("size"), 0.0)))
        if capacity <= 0.0:
            continue
        matches = [
            index
            for index, leg in enumerate(legs)
            if str(leg.get("token") or "") == str(trade.get("token") or "")
            and float(base.finite(trade.get("price"), math.inf))
            <= float(base.finite(leg.get("limit_price"), -math.inf)) + 1e-12
            and remaining[index] > 1e-12
        ]
        for index in matches:
            if capacity <= 1e-12:
                break
            queue_used = min(queues[index], capacity)
            queues[index] -= queue_used
            capacity -= queue_used
            own = min(remaining[index], capacity)
            remaining[index] -= own
            filled[index] += own
            capacity -= own

    mask = 0
    for index, value in enumerate(remaining):
        if value <= 1e-9:
            mask |= 1 << index
    return legs, filled, mask, (1 << len(legs)) - 1


def comparable_session(
    historical: dict[str, Any],
    current: dict[str, Any],
    *,
    edge_tolerance: float = 1e-12,
    size_tolerance_fraction: float = 0.02,
    burden_tolerance_fraction: float = 0.02,
    quote_tick_tolerance: float = 0.25,
) -> tuple[bool, list[str]]:
    """Conservative monotone transport check; no PnL is computed here.

    ``v7_graph_roundtrip_guard`` maps its v3 descriptor to this compatibility
    schema before calling the helper.  A historical session may authorize the
    current candidate only if history was not materially easier.
    """
    reasons: list[str] = []
    if historical.get("signature") != current.get("signature"):
        return False, ["signature"]
    if int(historical.get("evidence_version") or 0) != 2:
        return False, ["legacy_evidence_schema"]

    old = historical.get("execution_descriptor")
    new = current.get("execution_descriptor")
    if not isinstance(old, dict) or not isinstance(new, dict):
        return False, ["descriptor_missing"]
    if int(old.get("descriptor_version") or 0) != 2 or int(new.get("descriptor_version") or 0) != 2:
        reasons.append("descriptor_version")
    if int(old.get("window_seconds") or 0) != int(new.get("window_seconds") or 0):
        reasons.append("horizon")
    if str(old.get("quote_policy") or "") != str(new.get("quote_policy") or ""):
        reasons.append("quote_policy")
    old_contract = str(old.get("pnl_contract") or "")
    new_contract = str(new.get("pnl_contract") or "")
    if old_contract and new_contract and old_contract != new_contract:
        reasons.append("pnl_contract")

    old_edge = float(base.finite(old.get("expected_edge"), math.inf))
    new_edge = float(base.finite(new.get("expected_edge"), -math.inf))
    if old_edge > new_edge + max(0.0, edge_tolerance):
        reasons.append("historical_edge_too_favorable")

    old_notional = max(0.0, float(base.finite(old.get("max_notional"), 0.0)))
    new_notional = max(0.0, float(base.finite(new.get("max_notional"), 0.0)))
    size_mult = 1.0 - max(0.0, min(0.25, size_tolerance_fraction))
    if old_notional + 1e-12 < size_mult * new_notional:
        reasons.append("historical_notional_too_small")

    old_legs = {
        str(row.get("key") or ""): row
        for row in old.get("legs", [])
        if isinstance(row, dict)
    }
    new_legs = {
        str(row.get("key") or ""): row
        for row in new.get("legs", [])
        if isinstance(row, dict)
    }
    if set(old_legs) != set(new_legs) or not new_legs:
        return False, reasons + ["leg_set"]

    burden_mult = 1.0 - max(0.0, min(0.25, burden_tolerance_fraction))
    for key in sorted(new_legs):
        hist, curr = old_legs[key], new_legs[key]
        if float(base.finite(hist.get("target_shares"), 0.0)) + 1e-12 < (
            size_mult * float(base.finite(curr.get("target_shares"), 0.0))
        ):
            reasons.append(f"historical_target_too_small:{key}")
        if float(base.finite(hist.get("required_flow_to_target"), 0.0)) + 1e-12 < (
            burden_mult * float(base.finite(curr.get("required_flow_to_target"), 0.0))
        ):
            reasons.append(f"historical_queue_burden_too_easy:{key}")
        h_ticks = float(base.finite(hist.get("quote_ticks_from_bid"), math.inf))
        c_ticks = float(base.finite(curr.get("quote_ticks_from_bid"), -math.inf))
        if (
            not math.isfinite(h_ticks)
            or not math.isfinite(c_ticks)
            or abs(h_ticks - c_ticks) > max(0.0, quote_tick_tolerance)
        ):
            reasons.append(f"quote_ticks:{key}")
    return not reasons, reasons


def circular_block_bootstrap_lower(
    values: list[float],
    *,
    seed: int,
    reps: int,
    quantile: float,
    block_length: int,
) -> float:
    if not values:
        return -math.inf
    if len(values) < 8:
        return min(values)
    n = len(values)
    block = min(n, max(1, int(block_length)))
    rng = random.Random(seed)
    means: list[float] = []
    for _ in range(max(200, int(reps))):
        sample: list[float] = []
        while len(sample) < n:
            start = rng.randrange(n)
            sample.extend(values[(start + offset) % n] for offset in range(block))
        sample = sample[:n]
        means.append(sum(sample) / n)
    means.sort()
    q = max(0.0, min(0.49, float(quantile)))
    return means[min(len(means) - 1, max(0, int(q * (len(means) - 1))))]


def dependence_block_length(rows: list[dict[str, Any]], window_seconds: int) -> int:
    n = len(rows)
    if n <= 1:
        return 1
    origins = sorted(
        int(row.get("origin_received_ms") or 0)
        for row in rows
        if int(row.get("origin_received_ms") or 0) > 0
    )
    spacings = [b - a for a, b in zip(origins, origins[1:]) if b > a]
    overlap = 1
    if spacings:
        median_spacing = statistics.median(spacings)
        overlap = max(
            1,
            math.ceil(max(1, int(window_seconds)) * 1000 / max(1.0, median_spacing)),
        )
    return min(n, max(overlap, 2 if n >= 8 else 1, math.ceil(math.sqrt(n))))


def effective_nonoverlap_sessions(rows: list[dict[str, Any]], window_seconds: int) -> int:
    window_ms = max(1, int(window_seconds)) * 1000
    count = 0
    next_allowed = -1
    for row in sorted(rows, key=lambda item: int(item.get("origin_received_ms") or 0)):
        origin = int(row.get("origin_received_ms") or 0)
        if origin <= 0 or origin < next_allowed:
            continue
        count += 1
        next_allowed = origin + window_ms
    return count


if __name__ == "__main__":
    raise SystemExit("helper-only module; canonical runtime owner is v7_graph_roundtrip_guard.py")
