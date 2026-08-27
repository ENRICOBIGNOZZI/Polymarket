#!/usr/bin/env python3
"""Causal schema and label construction for V7 learned execution."""
from __future__ import annotations

import math
from collections import Counter, defaultdict
from dataclasses import dataclass
from typing import Any, Sequence

import v7_learned_execution_model_base as b

ExecutionModelError = b.ExecutionModelError
get = b.get
finite_optional = b.finite_optional
required_finite = b.required_finite
optional_value = b.optional_value
event_ts = b.event_ts
SHA_RE = b.SHA_RE
TERMINAL = b.TERMINAL
MARKOUTS = b.MARKOUTS
HORIZON_MS = {"1s": 1_000, "10s": 10_000, "45s": 45_000, "60s": 60_000, "300s": 300_000}
FEATURES = (
    "log_queue_plus_one", "log_bid_depth_plus_one", "log_ask_depth_plus_one",
    "spread", "book_imbalance", "size_to_min_touch_depth",
    "receive_exchange_latency_s", "decision_receive_latency_s", "limit_distance_to_passive_touch",
    "side_buy", "side_sell", "timeout_s", "timeout_present",
    "predicted_alpha", "predicted_alpha_present", "expected_ev", "expected_ev_present",
    "action_cross", "action_join", "action_improve", "action_fade",
    "action_taker", "action_maker", "action_cancel_replace",
)
JOINT_SUMMARY_FEATURES = (
    "leg_count", "decision_skew_s", "max_log_queue_plus_one", "max_size_to_min_touch_depth",
)


@dataclass(frozen=True)
class OrderExample:
    order_id: str
    strategy: str
    token_id: str
    side: str
    bundle_id: str | None
    leg_id: str | None
    expected_leg_count: int | None
    expected_leg_ids: tuple[str, ...] | None
    ts_ms: int
    label_ts_ms: int
    x: tuple[float, ...]
    fill: int
    complete: int
    state: str
    markouts: dict[str, float]
    markout_ts_ms: dict[str, int]


@dataclass(frozen=True)
class JointExample:
    group_id: str
    strategy: str
    leg_signature: tuple[str, ...]
    leg_count: int
    ts_ms: int
    label_ts_ms: int
    x: tuple[float, ...]
    state: str


@dataclass(frozen=True)
class FillInfo:
    quantity: float
    strategy: str
    recorded_ts_ms: int
    exchange_ts_ms: int
    token_id: str
    side: str
    leg_id: str | None
    bundle_id: str | None


def _identity(event: Any) -> tuple[str, str, str | None, str | None]:
    return (
        str(get(event, "token_id", "") or "").strip(),
        str(get(event, "side", "") or "").upper().strip(),
        str(get(event, "leg_id", "") or "") or None,
        str(get(event, "bundle_id", "") or "") or None,
    )


def bundle_contract(event: Any) -> tuple[int | None, tuple[str, ...] | None]:
    """Return an explicit immutable bundle contract from submission metadata."""
    if not str(get(event, "bundle_id", "") or "").strip():
        return None, None
    metadata = get(event, "metadata", {}) or {}
    if not isinstance(metadata, dict):
        return None, None
    count = metadata.get("expected_leg_count")
    if isinstance(count, bool) or not isinstance(count, int) or count < 2:
        return None, None
    raw_ids = metadata.get("expected_leg_ids")
    if not isinstance(raw_ids, (list, tuple)) or len(raw_ids) != count:
        return count, None
    ids = tuple(str(value).strip() for value in raw_ids)
    if any(not value for value in ids) or len(set(ids)) != len(ids):
        return count, None
    return count, ids


def features(event: Any) -> tuple[float, ...]:
    if not str(get(event, "book_snapshot_id", "") or "").strip():
        raise ExecutionModelError("features:missing_book_snapshot_id")
    token_id, side, _, _ = _identity(event)
    if not token_id or side not in {"BUY", "SELL"}:
        raise ExecutionModelError("features:missing_instrument_or_side")
    bid = required_finite(event, "bid", nonnegative=True)
    ask = required_finite(event, "ask", nonnegative=True)
    bd = required_finite(event, "bid_depth", nonnegative=True)
    ad = required_finite(event, "ask_depth", nonnegative=True)
    queue = required_finite(event, "queue_ahead", nonnegative=True)
    size = required_finite(event, "intended_size", positive=True)
    limit_price = required_finite(event, "limit_price", nonnegative=True)
    if bid > ask or not 0.0 <= limit_price <= 1.0:
        raise ExecutionModelError("features:invalid_book_or_limit")
    exchange = int(get(event, "exchange_ts_ms", 0) or 0)
    receive = int(get(event, "receive_ts_ms", 0) or 0)
    decision = int(get(event, "decision_ts_ms", 0) or 0)
    recorded = event_ts(event)
    if exchange <= 0 or receive < exchange or decision < receive or recorded < decision:
        raise ExecutionModelError("features:invalid_exchange_receive_decision_recorded_clock")
    action = str(get(event, "intended_action", "") or "").upper().strip()
    if not action:
        raise ExecutionModelError("features:missing_action")
    timeout, timeout_present = optional_value(event, "timeout_ms", nonnegative=True)
    alpha, alpha_present = optional_value(event, "predicted_alpha")
    ev, ev_present = optional_value(event, "expected_ev")
    touch = min(bd, ad) if bd > 0.0 and ad > 0.0 else max(bd, ad)
    depth = bd + ad
    if touch <= 0.0 or depth <= 0.0:
        raise ExecutionModelError("features:zero_executable_depth")
    passive_distance = limit_price - bid if side == "BUY" else ask - limit_price
    flags = tuple(float(token in action) for token in ("CROSS", "JOIN", "IMPROVE", "FADE", "TAKER", "MAKER"))
    return (
        math.log1p(queue), math.log1p(bd), math.log1p(ad), ask - bid,
        (bd - ad) / depth, size / touch, (receive - exchange) / 1000.0,
        (decision - receive) / 1000.0, passive_distance,
        float(side == "BUY"), float(side == "SELL"),
        timeout / 1000.0, timeout_present, alpha, alpha_present, ev, ev_present,
        *flags, float("CANCEL" in action or "REPLACE" in action),
    )


def validate_stream(events: Sequence[Any], sha: str) -> None:
    if not SHA_RE.fullmatch(sha):
        raise ExecutionModelError("model_sha:not_exact_git_sha")
    for i, event in enumerate(events, 1):
        if str(get(event, "model_sha", "")) != sha:
            raise ExecutionModelError(f"event_{i}:mixed_sha")
        if get(event, "paper_only", None) is not True:
            raise ExecutionModelError(f"event_{i}:not_paper_only")
        if get(event, "authenticated_execution", None) is not False:
            raise ExecutionModelError(f"event_{i}:authenticated_execution_forbidden")


def build_orders(events: Sequence[Any], sha: str) -> tuple[list[OrderExample], dict[str, int]]:
    validate_stream(events, sha)
    submitted: dict[str, Any] = {}
    later: dict[str, list[Any]] = defaultdict(list)
    stats: Counter[str] = Counter()
    for event in events:
        typ = str(get(event, "event_type", "")).upper()
        oid = str(get(event, "order_id", "") or "")
        if typ == "ORDER_SUBMITTED":
            if not oid or oid in submitted:
                raise ExecutionModelError("submission:missing_or_duplicate_order_id")
            submitted[oid] = event
            stats["submissions"] += 1
        elif oid:
            later[oid].append(event)

    out: list[OrderExample] = []
    for oid, sub in submitted.items():
        try:
            x = features(sub)
        except ExecutionModelError:
            stats["excluded_missing_or_invalid_features"] += 1
            continue
        intended = required_finite(sub, "intended_size", positive=True)
        decision_ts = int(get(sub, "decision_ts_ms", 0) or 0)
        strategy = str(get(sub, "strategy", "") or "").strip()
        token_id, side, leg, bundle = _identity(sub)
        expected_leg_count, expected_leg_ids = bundle_contract(sub)
        filled = 0.0
        terminal = False
        explicit_complete = False
        resolution_ts = 0
        fill_info: dict[str, FillInfo] = {}
        order_events = later.get(oid, [])

        for event in order_events:
            typ = str(get(event, "event_type", "")).upper()
            state = str(get(event, "order_state", "") or "").upper()
            ts = event_ts(event)
            if typ == "FILL":
                fid = str(get(event, "fill_id", "") or "")
                if not fid or fid in fill_info:
                    raise ExecutionModelError(f"fill:missing_or_duplicate_fill_id:{oid}")
                qty = required_finite(event, "filled_size", positive=True)
                ex_ts = int(get(event, "exchange_ts_ms", 0) or 0)
                recv_ts = int(get(event, "receive_ts_ms", 0) or 0)
                if ex_ts <= 0 or recv_ts < ex_ts or ts < recv_ts:
                    raise ExecutionModelError(f"fill:invalid_exchange_receive_recorded_clock:{fid}")
                fill_identity = _identity(event)
                if (str(get(event, "strategy", "") or "").strip(), *fill_identity) != (strategy, token_id, side, leg, bundle):
                    raise ExecutionModelError(f"fill:lineage_mismatch:{fid}")
                fill_info[fid] = FillInfo(qty, strategy, ts, ex_ts, *fill_identity)
                filled += qty
                if filled > intended * (1.0 + 1e-9):
                    raise ExecutionModelError(f"fill:overfill:{oid}")
                if get(event, "complete", None) is True:
                    explicit_complete = terminal = True
                    resolution_ts = max(resolution_ts, ts)
                if filled >= intended * (1.0 - 1e-9):
                    terminal = True
                    resolution_ts = max(resolution_ts, ts)
            if state in TERMINAL:
                terminal = True
                resolution_ts = max(resolution_ts, ts)
            if get(event, "complete", None) is True:
                explicit_complete = True

        if explicit_complete and filled < intended * (1.0 - 1e-9):
            raise ExecutionModelError(f"fill:complete_below_intended:{oid}")
        complete = filled >= intended * (1.0 - 1e-9)
        if complete and resolution_ts <= 0:
            resolution_ts = max((info.recorded_ts_ms for info in fill_info.values()), default=0)
        if not terminal or resolution_ts <= 0:
            stats["unresolved_orders"] += 1
            continue

        num: Counter[str] = Counter()
        den: Counter[str] = Counter()
        coverage: dict[str, set[str]] = defaultdict(set)
        markout_ts: dict[str, int] = {}
        seen: set[tuple[str, str]] = set()
        for event in order_events:
            if str(get(event, "event_type", "")).upper() != "MARKOUT":
                continue
            fid = str(get(event, "fill_id", "") or "")
            if fid not in fill_info:
                raise ExecutionModelError(f"markout:orphan_fill_id:{fid or 'missing'}")
            info = fill_info[fid]
            if (str(get(event, "strategy", "") or "").strip(), *_identity(event)) != (info.strategy, info.token_id, info.side, info.leg_id, info.bundle_id):
                raise ExecutionModelError(f"markout:lineage_mismatch:{fid}")
            marks = get(event, "markouts", {}) or {}
            if not isinstance(marks, dict) or len(marks) != 1:
                raise ExecutionModelError(f"markout:invalid_horizon_payload:{fid}")
            horizon, raw = next(iter(marks.items()))
            if horizon not in MARKOUTS or (fid, horizon) in seen:
                raise ExecutionModelError(f"markout:unsupported_or_duplicate:{fid}:{horizon}")
            seen.add((fid, horizon))
            value = finite_optional(raw)
            if value is None:
                raise ExecutionModelError(f"markout:nonfinite:{fid}:{horizon}")
            recorded = event_ts(event)
            exchange = int(get(event, "exchange_ts_ms", 0) or 0)
            receive = int(get(event, "receive_ts_ms", 0) or 0)
            if exchange <= 0 or receive < exchange or recorded < receive or recorded < info.recorded_ts_ms or exchange < info.exchange_ts_ms:
                raise ExecutionModelError(f"markout:noncausal_clock:{fid}:{horizon}")
            maturity = info.exchange_ts_ms + HORIZON_MS[horizon]
            if exchange < maturity or receive < maturity:
                raise ExecutionModelError(f"markout:not_mature:{fid}:{horizon}")
            num[horizon] += info.quantity * value
            den[horizon] += info.quantity
            coverage[horizon].add(fid)
            markout_ts[horizon] = max(markout_ts.get(horizon, 0), receive, recorded)

        fill_ids = set(fill_info)
        markouts: dict[str, float] = {}
        for horizon in MARKOUTS:
            covered = coverage.get(horizon, set())
            if not covered:
                continue
            if covered != fill_ids:
                stats[f"incomplete_markout_coverage_{horizon}"] += 1
                continue
            markouts[horizon] = num[horizon] / den[horizon]

        state = "COMPLETE" if complete else "PARTIAL" if filled > 0.0 else "NO_FILL"
        out.append(OrderExample(
            oid, strategy, token_id, side, bundle, leg, expected_leg_count, expected_leg_ids,
            decision_ts, resolution_ts, x, int(filled > 0.0), int(complete), state, markouts, markout_ts,
        ))
        stats[f"state_{state.lower()}"] += 1
    out.sort(key=lambda row: (row.ts_ms, row.order_id))
    stats["resolved_feature_complete_orders"] = len(out)
    return out, dict(stats)


def joint_feature_names(nlegs: int, leg_ids: Sequence[str] | None = None) -> tuple[str, ...]:
    labels = list(leg_ids) if leg_ids is not None else [f"position_{i + 1}" for i in range(nlegs)]
    if len(labels) != nlegs:
        raise ExecutionModelError("joint_feature_names:bad_leg_count")
    return tuple(f"leg[{leg_id}]_{name}" for leg_id in labels for name in FEATURES) + JOINT_SUMMARY_FEATURES


def build_joint(orders: Sequence[OrderExample]) -> tuple[list[JointExample], dict[str, int]]:
    groups: dict[str, list[OrderExample]] = defaultdict(list)
    stats: Counter[str] = Counter()
    for order in orders:
        if order.bundle_id:
            groups[order.bundle_id].append(order)
        else:
            stats["skipped_missing_bundle_id"] += 1
    out: list[JointExample] = []
    for gid, raw in groups.items():
        if len({leg.strategy for leg in raw}) != 1:
            stats["skipped_mixed_strategy_bundle"] += 1
            continue
        if any(not leg.leg_id for leg in raw) or len({leg.leg_id for leg in raw}) != len(raw):
            stats["skipped_missing_or_duplicate_leg_id"] += 1
            continue
        counts = {leg.expected_leg_count for leg in raw}
        signatures = {leg.expected_leg_ids for leg in raw}
        if None in counts:
            stats["skipped_missing_bundle_contract"] += 1
            continue
        if len(counts) != 1 or len(signatures) != 1:
            stats["skipped_conflicting_bundle_contract"] += 1
            continue
        expected = int(next(iter(counts)))
        signature = next(iter(signatures))
        if signature is None:
            stats["skipped_missing_leg_signature"] += 1
            continue
        if len(raw) != expected:
            stats["skipped_incomplete_bundle"] += 1
            continue
        by_id = {str(leg.leg_id): leg for leg in raw}
        if set(signature) != set(by_id):
            stats["skipped_incomplete_bundle"] += 1
            continue
        legs = [by_id[leg_id] for leg_id in signature]
        decision_skew = (max(leg.ts_ms for leg in legs) - min(leg.ts_ms for leg in legs)) / 1000.0
        x = tuple(value for leg in legs for value in leg.x) + (
            float(expected), decision_skew, max(leg.x[0] for leg in legs), max(leg.x[5] for leg in legs),
        )
        out.append(JointExample(
            gid, legs[0].strategy, signature, expected, max(leg.ts_ms for leg in legs),
            max(leg.label_ts_ms for leg in legs), x, "|".join(leg.state for leg in legs),
        ))
    out.sort(key=lambda row: (row.ts_ms, row.group_id))
    stats["joint_examples"] = len(out)
    return out, dict(stats)


