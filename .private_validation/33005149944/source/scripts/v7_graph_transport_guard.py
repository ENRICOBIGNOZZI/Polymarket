#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import math
import time
from collections import Counter, defaultdict
from pathlib import Path
from typing import Any

import v7_graph_forward_guard as base

SCHEMA = "v7_graph_transport_guard_v1"


def empty_state() -> dict[str, Any]:
    return {
        "schema": SCHEMA,
        "paper_only": True,
        "authenticated_execution": False,
        "open": [],
        "completed": [],
        "invalid": [],
    }


def read_state(path: Path) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return empty_state()
    if not isinstance(value, dict) or value.get("schema") != SCHEMA:
        return empty_state()
    state = empty_state()
    for key in ("open", "completed", "invalid"):
        rows = value.get(key)
        if isinstance(rows, list):
            state[key] = [dict(row) for row in rows if isinstance(row, dict)]
    return state


def _leg_key(leg: dict[str, Any]) -> str:
    return f"{str(leg.get('market_id') or '')}:{str(leg.get('side') or '').upper()}"


def attach_transport_descriptor(session: dict[str, Any], clob: str, window_seconds: int) -> tuple[dict[str, Any] | None, str]:
    """Freeze the economic/execution state used to judge evidence transport.

    Historical sessions may only authorize a current candidate when the historical
    state was no easier: no larger edge, no smaller sizing, no smaller normalized
    queue burden, and the same maker quote policy/horizon.
    """
    legs = [dict(leg) for leg in session.get("legs", []) if isinstance(leg, dict)]
    if not legs:
        return None, "transport_no_legs"
    tokens = [str(leg.get("token") or "") for leg in legs]
    books, descriptor_received_ms = base.fetch_books(clob, tokens)
    if any(token not in books for token in tokens):
        return None, "transport_book_missing"

    descriptor_legs: list[dict[str, Any]] = []
    for leg in legs:
        token = str(leg["token"])
        book = books[token]
        target = max(0.0, base.finite(leg.get("target_shares"), 0.0))
        queue = max(0.0, base.finite(leg.get("queue_ahead"), 0.0))
        required = max(0.0, base.finite(leg.get("required_flow"), queue + target))
        tick = max(1e-9, base.finite(book.get("tick"), 0.01))
        bid = base.finite(book.get("bid"))
        limit = base.finite(leg.get("limit_price"))
        if target <= 0.0 or not math.isfinite(bid) or not math.isfinite(limit):
            return None, "transport_invalid_leg"
        quote_ticks = (limit - bid) / tick
        descriptor_legs.append({
            "key": _leg_key(leg),
            "target_shares": target,
            "queue_ahead": queue,
            "required_flow": required,
            "required_flow_to_target": required / max(target, 1e-12),
            "queue_to_target": queue / max(target, 1e-12),
            "quote_ticks_from_bid": quote_ticks,
        })
    descriptor_legs.sort(key=lambda row: row["key"])
    session = dict(session)
    session["transport"] = {
        "descriptor_version": 1,
        "descriptor_received_ms": descriptor_received_ms,
        "window_seconds": int(window_seconds),
        "quote_policy": "maker_limit_ticks_from_bid",
        "expected_edge": float(base.finite(session.get("expected_edge"), 0.0)),
        "max_notional": max(0.0, float(base.finite(session.get("max_notional"), 0.0))),
        "legs": descriptor_legs,
    }
    return session, "transport_registered"


def mature_session(
    session: dict[str, Any],
    tape: list[dict[str, Any]],
    gamma: str,
    clob: str,
    slippage_bps: float,
) -> dict[str, Any] | None:
    outcome = base.mature_session(session, tape, gamma, clob, slippage_bps)
    if outcome is None:
        return None
    transport = session.get("transport")
    if not isinstance(transport, dict):
        return None
    outcome = dict(outcome)
    outcome["transport"] = dict(transport)
    outcome["expected_edge"] = float(base.finite(session.get("expected_edge"), 0.0))
    outcome["max_notional"] = max(0.0, float(base.finite(session.get("max_notional"), 0.0)))
    return outcome


def comparable_session(
    historical: dict[str, Any],
    current: dict[str, Any],
    *,
    edge_tolerance: float = 1e-12,
    size_tolerance_fraction: float = 0.02,
    burden_tolerance_fraction: float = 0.02,
    quote_tick_tolerance: float = 0.25,
) -> tuple[bool, list[str]]:
    reasons: list[str] = []
    if historical.get("signature") != current.get("signature"):
        reasons.append("signature")
        return False, reasons
    old = historical.get("transport")
    new = current.get("transport")
    if not isinstance(old, dict) or not isinstance(new, dict):
        reasons.append("descriptor_missing")
        return False, reasons
    if int(old.get("descriptor_version") or 0) != 1 or int(new.get("descriptor_version") or 0) != 1:
        reasons.append("descriptor_version")
    if int(old.get("window_seconds") or 0) != int(new.get("window_seconds") or 0):
        reasons.append("horizon")
    if str(old.get("quote_policy") or "") != str(new.get("quote_policy") or ""):
        reasons.append("quote_policy")

    old_edge = float(base.finite(old.get("expected_edge"), math.inf))
    new_edge = float(base.finite(new.get("expected_edge"), -math.inf))
    # A historical state with more edge is economically easier and cannot
    # validate a lower-edge current candidate.
    if old_edge > new_edge + max(0.0, edge_tolerance):
        reasons.append("historical_edge_too_favorable")

    old_notional = max(0.0, float(base.finite(old.get("max_notional"), 0.0)))
    new_notional = max(0.0, float(base.finite(new.get("max_notional"), 0.0)))
    size_floor = (1.0 - max(0.0, min(0.25, size_tolerance_fraction))) * new_notional
    if old_notional + 1e-12 < size_floor:
        reasons.append("historical_notional_too_small")

    old_legs = {str(row.get("key") or ""): row for row in old.get("legs", []) if isinstance(row, dict)}
    new_legs = {str(row.get("key") or ""): row for row in new.get("legs", []) if isinstance(row, dict)}
    if set(old_legs) != set(new_legs) or not new_legs:
        reasons.append("leg_set")
        return False, reasons

    size_mult = 1.0 - max(0.0, min(0.25, size_tolerance_fraction))
    burden_mult = 1.0 - max(0.0, min(0.25, burden_tolerance_fraction))
    for key in sorted(new_legs):
        hist = old_legs[key]
        curr = new_legs[key]
        h_target = max(0.0, float(base.finite(hist.get("target_shares"), 0.0)))
        c_target = max(0.0, float(base.finite(curr.get("target_shares"), 0.0)))
        if h_target + 1e-12 < size_mult * c_target:
            reasons.append(f"historical_target_too_small:{key}")
        h_burden = max(0.0, float(base.finite(hist.get("required_flow_to_target"), 0.0)))
        c_burden = max(0.0, float(base.finite(curr.get("required_flow_to_target"), 0.0)))
        if h_burden + 1e-12 < burden_mult * c_burden:
            reasons.append(f"historical_queue_burden_too_easy:{key}")
        h_ticks = float(base.finite(hist.get("quote_ticks_from_bid"), math.inf))
        c_ticks = float(base.finite(curr.get("quote_ticks_from_bid"), -math.inf))
        if not math.isfinite(h_ticks) or not math.isfinite(c_ticks) or abs(h_ticks - c_ticks) > max(0.0, quote_tick_tolerance):
            reasons.append(f"quote_ticks:{key}")
    return not reasons, reasons


def evidence_for(
    current: dict[str, Any],
    completed: list[dict[str, Any]],
    min_sessions: int,
    reps: int,
    quantile: float,
) -> dict[str, Any]:
    signature_value = str(current.get("signature") or "")
    structural = [row for row in completed if row.get("signature") == signature_value][-200:]
    comparable: list[dict[str, Any]] = []
    rejected = Counter()
    for row in structural:
        ok, reasons = comparable_session(row, current)
        if ok:
            comparable.append(row)
        else:
            for reason in reasons:
                rejected[reason] += 1
    rows = comparable[-100:]
    result: dict[str, Any] = {
        "structural_sessions": len(structural),
        "comparable_sessions": len(rows),
        "rejected_by_transport": dict(rejected),
        "full_completions": sum(bool(row.get("full_completion")) for row in rows),
    }
    result["full_completion_probability"] = result["full_completions"] / len(rows) if rows else 0.0
    result["stress"] = {}
    accepted = len(rows) >= min_sessions and result["full_completions"] > 0
    for mult in ("1x", "1.5x", "2x"):
        values = [float(row["stress_pnl"][mult]) for row in rows if row.get("stress_pnl", {}).get(mult) is not None]
        lower = base.bootstrap_lower(values, 20260826 + sum(ord(ch) for ch in signature_value + mult), reps, quantile)
        mean = sum(values) / len(values) if values else -math.inf
        result["stress"][mult] = {"mean_pnl": mean, "bootstrap_lower": lower, "n": len(values)}
        accepted = accepted and len(values) >= min_sessions and lower > 0.0
    result["accepted"] = bool(accepted)
    return result


def main() -> int:
    parser = argparse.ArgumentParser(description="State-comparable prospective Graph/RV PAPER admission guard")
    parser.add_argument("--config", type=Path, required=True)
    parser.add_argument("--input", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--state", type=Path, required=True)
    parser.add_argument("--status", type=Path, required=True)
    parser.add_argument("--trade-tape", type=Path, required=True)
    parser.add_argument("--window-seconds", type=int, default=180)
    parser.add_argument("--min-sessions", type=int, default=4)
    parser.add_argument("--slippage-bps", type=float, default=5.0)
    parser.add_argument("--bootstrap-reps", type=int, default=400)
    parser.add_argument("--bootstrap-quantile", type=float, default=0.10)
    args = parser.parse_args()

    cfg = json.loads(args.config.read_text(encoding="utf-8"))
    gamma, clob = str(cfg["gamma_url"]), str(cfg["clob_url"])
    state = read_state(args.state)
    current_ms = base.now_ms()
    tape = base.tape_rows(args.trade_tape)
    open_sessions: list[dict[str, Any]] = []
    completed = [dict(row) for row in state["completed"] if isinstance(row, dict)]
    invalid = [dict(row) for row in state["invalid"] if isinstance(row, dict)]

    for session in [dict(row) for row in state["open"] if isinstance(row, dict)]:
        if current_ms < int(session.get("deadline_received_ms") or 0):
            open_sessions.append(session)
            continue
        outcome = mature_session(session, tape, gamma, clob, args.slippage_bps)
        if outcome is None:
            invalid.append({
                "session_id": session.get("session_id"),
                "signature": session.get("signature"),
                "reason": "transport_descriptor_or_unwind_unavailable",
                "timestamp": int(time.time()),
            })
        else:
            completed.append(outcome)
    completed = completed[-2000:]
    invalid = invalid[-500:]

    grouped: dict[str, list[dict[str, str]]] = defaultdict(list)
    for row in base.read_csv(args.input):
        bundle_id = str(row.get("bundle_id") or "")
        if bundle_id:
            grouped[bundle_id].append(row)

    accepted_rows: list[dict[str, Any]] = []
    decisions: list[dict[str, Any]] = []
    open_signatures = {str(session.get("signature") or "") for session in open_sessions}
    for bundle_id, rows in grouped.items():
        sig = base.signature(rows)
        current, reason = base.snapshot_candidate(rows, gamma, clob, args.window_seconds)
        if current is not None:
            current, transport_reason = attach_transport_descriptor(current, clob, args.window_seconds)
            if current is None:
                reason = transport_reason
        decision: dict[str, Any] = {
            "bundle_id": bundle_id,
            "signature": sig,
            "snapshot": reason,
            "routed": False,
        }
        if current is not None:
            evidence = evidence_for(current, completed, args.min_sessions, args.bootstrap_reps, args.bootstrap_quantile)
            decision["evidence"] = evidence
            if evidence["accepted"]:
                accepted_rows.extend(rows)
                decision["routed"] = True
            if sig not in open_signatures:
                open_sessions.append(current)
                open_signatures.add(sig)
                decision["registration"] = "registered_current_state"
            else:
                decision["registration"] = "existing_open_session"
        else:
            decision["evidence"] = {
                "structural_sessions": 0,
                "comparable_sessions": 0,
                "accepted": False,
                "reason": "current_snapshot_unavailable",
            }
            decision["registration"] = "not_registered"
        decisions.append(decision)

    new_state = {
        "schema": SCHEMA,
        "paper_only": True,
        "authenticated_execution": False,
        "updated_ms": current_ms,
        "open": open_sessions,
        "completed": completed,
        "invalid": invalid,
    }
    base.atomic_json(args.state, new_state)
    base.atomic_csv(args.output, accepted_rows)
    status = {
        "schema": SCHEMA,
        "timestamp": int(time.time()),
        "paper_only": True,
        "authenticated_execution": False,
        "prospective_only": True,
        "point_in_time_queue_snapshot": True,
        "state_comparable_transport": True,
        "input_bundles": len(grouped),
        "routed_bundles": len({row["bundle_id"] for row in accepted_rows}),
        "open_sessions": len(open_sessions),
        "completed_sessions": len(completed),
        "invalid_sessions": len(invalid),
        "minimum_comparable_sessions": args.min_sessions,
        "decisions": decisions,
        "contracts": [
            "same_structural_signature",
            "same_horizon",
            "same_quote_ticks_policy",
            "historical_edge_not_more_favorable",
            "historical_size_not_smaller",
            "historical_queue_burden_not_easier",
            "prospective_dual_clock_joint_state",
            "partial_state_contemporaneous_unwind",
            "cost_stress_1x_1.5x_2x",
            "positive_bootstrap_lower_before_route",
        ],
    }
    base.atomic_json(args.status, status)
    print(json.dumps(status, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
