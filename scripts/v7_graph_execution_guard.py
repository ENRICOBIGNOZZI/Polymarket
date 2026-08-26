#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import math
import random
import statistics
import time
from collections import Counter, defaultdict
from pathlib import Path
from typing import Any

import v7_graph_forward_guard as base

SCHEMA = "v7_graph_execution_guard_v2"
STRESSES = (("1x", 1.0), ("1.5x", 1.5), ("2x", 2.0))


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


def _event_market_ids(event: dict[str, Any]) -> set[str]:
    rows = event.get("markets")
    if not isinstance(rows, list):
        return set()
    return {
        str(row.get("id") or "")
        for row in rows
        if isinstance(row, dict) and str(row.get("id") or "")
    }


def verified_terminal_payout_floor(session: dict[str, Any], gamma: str) -> tuple[float | None, str]:
    """Prove a terminal floor rather than treating quoted edge as realized PnL.

    The currently live Graph/RV source is a complete non-augmented NegRisk set
    relabeled from GRAPH_HARD to GRAPH_RV because maker legging destroys immediate
    arbitrage. Once every YES leg is actually filled at equal matched units, the
    complete set still has a structural terminal payout floor. Anything that is
    not provably that object fails closed and needs fixed-horizon executable
    markout evidence instead of this terminal-settlement path.
    """
    if str(session.get("strategy") or "") != "GRAPH_RV":
        return None, "non_guaranteed_rv_requires_executable_markout"
    event_id = str(session.get("event_id") or "")
    if not event_id:
        return None, "missing_event"
    try:
        event = base.request_json(f"{gamma.rstrip('/')}/events/{event_id}")
    except Exception:
        return None, "event_fetch"
    if not isinstance(event, dict) or not bool(event.get("negRisk")) or bool(event.get("negRiskAugmented")):
        return None, "not_complete_nonaugmented_negrisk"
    legs = [dict(row) for row in session.get("legs", []) if isinstance(row, dict)]
    if len(legs) < 2 or any(str(leg.get("side") or "").upper() != "YES" for leg in legs):
        return None, "complete_set_requires_yes_legs"
    leg_markets = {str(leg.get("market_id") or "") for leg in legs}
    event_markets = _event_market_ids(event)
    if not event_markets or leg_markets != event_markets:
        return None, "incomplete_event_market_set"
    weights = [max(0.0, float(base.finite(leg.get("weight"), 0.0))) for leg in legs]
    if any(weight <= 0.0 for weight in weights):
        return None, "invalid_weights"
    return min(weights), "verified_complete_nonaugmented_negrisk"


def attach_execution_descriptor(
    session: dict[str, Any], gamma: str, clob: str, window_seconds: int
) -> tuple[dict[str, Any] | None, str]:
    legs = [dict(leg) for leg in session.get("legs", []) if isinstance(leg, dict)]
    if not legs:
        return None, "descriptor_no_legs"
    floor_per_unit, floor_reason = verified_terminal_payout_floor(session, gamma)
    if floor_per_unit is None:
        return None, floor_reason

    tokens = [str(leg.get("token") or "") for leg in legs]
    books, descriptor_received_ms = base.fetch_books(clob, tokens)
    if any(token not in books for token in tokens):
        return None, "descriptor_book_missing"

    descriptor_legs: list[dict[str, Any]] = []
    base_entry_cash = 0.0
    entry_fee_cash = 0.0
    for leg in legs:
        token = str(leg["token"])
        book = books[token]
        target = max(0.0, float(base.finite(leg.get("target_shares"), 0.0)))
        queue = max(0.0, float(base.finite(leg.get("queue_ahead"), 0.0)))
        required = max(0.0, float(base.finite(leg.get("required_flow"), queue + target)))
        tick = max(1e-9, float(base.finite(book.get("tick"), 0.01)))
        bid = float(base.finite(book.get("bid")))
        limit = float(base.finite(leg.get("limit_price")))
        fee_per_share = max(0.0, float(base.finite(leg.get("entry_fee_per_share"), math.nan)))
        if target <= 0.0 or not math.isfinite(bid) or not math.isfinite(limit) or not math.isfinite(fee_per_share):
            return None, "descriptor_invalid_leg"
        base_entry_cash += target * limit
        entry_fee_cash += target * fee_per_share
        descriptor_legs.append({
            "key": _leg_key(leg),
            "target_shares": target,
            "queue_ahead": queue,
            "required_flow": required,
            "required_flow_to_target": required / max(target, 1e-12),
            "queue_to_target": queue / max(target, 1e-12),
            "quote_ticks_from_bid": (limit - bid) / tick,
        })
    descriptor_legs.sort(key=lambda row: row["key"])

    created_ts = int(base.finite(session.get("created_ts"), int(time.time())))
    hold_deadlines = [
        int(base.finite(row.get("hold_deadline_ts"), created_ts))
        for row in session.get("intent_rows", [])
        if isinstance(row, dict)
    ]
    hold_seconds = max(0, max(hold_deadlines, default=created_ts) - created_ts)
    units = max(0.0, float(base.finite(session.get("units"), 0.0)))
    max_notional = max(0.0, float(base.finite(session.get("max_notional"), 0.0)))
    if units <= 0.0 or max_notional <= 0.0:
        return None, "descriptor_invalid_notional"

    enriched = dict(session)
    enriched["execution_descriptor"] = {
        "descriptor_version": 2,
        "descriptor_received_ms": descriptor_received_ms,
        "window_seconds": int(window_seconds),
        "quote_policy": "maker_limit_ticks_from_bid",
        "expected_edge": float(base.finite(session.get("expected_edge"), 0.0)),
        "max_notional": max_notional,
        "terminal_floor_reason": floor_reason,
        "terminal_payout_floor_per_unit": float(floor_per_unit),
        "terminal_payout_floor_cash": units * float(floor_per_unit),
        "base_entry_cash": base_entry_cash,
        "entry_fee_cash_1x": entry_fee_cash,
        "hold_seconds": hold_seconds,
        "legs": descriptor_legs,
    }
    return enriched, "execution_descriptor_registered"


def full_completion_stress_pnl(
    session: dict[str, Any], cost_multiplier: float, capital_cost_bps_per_hour: float
) -> float | None:
    descriptor = session.get("execution_descriptor")
    if not isinstance(descriptor, dict) or int(descriptor.get("descriptor_version") or 0) != 2:
        return None
    payout = float(base.finite(descriptor.get("terminal_payout_floor_cash"), math.nan))
    entry_cash = float(base.finite(descriptor.get("base_entry_cash"), math.nan))
    fees = float(base.finite(descriptor.get("entry_fee_cash_1x"), math.nan))
    hold_seconds = max(0.0, float(base.finite(descriptor.get("hold_seconds"), 0.0)))
    if not all(math.isfinite(value) and value >= 0.0 for value in (payout, entry_cash, fees)):
        return None
    rate = max(0.0, float(capital_cost_bps_per_hour)) / 10000.0
    capital_time_1x = (entry_cash + fees) * rate * hold_seconds / 3600.0
    mult = max(0.0, float(cost_multiplier))
    return payout - entry_cash - mult * (fees + capital_time_1x)


def _simulate_fills(session: dict[str, Any], tape: list[dict[str, Any]]) -> tuple[list[dict[str, Any]], list[float], int, int]:
    legs = [dict(leg) for leg in session.get("legs", []) if isinstance(leg, dict)]
    queues = [max(0.0, float(base.finite(leg.get("queue_ahead"), 0.0))) for leg in legs]
    remaining = [max(0.0, float(base.finite(leg.get("target_shares"), 0.0))) for leg in legs]
    filled = [0.0] * len(legs)
    for trade in tape:
        if not (int(session["origin_received_ms"]) < int(trade["received_ms"]) <= int(session["deadline_received_ms"])):
            continue
        if not (int(session["origin_event_ms"]) < int(trade["event_ms"]) <= int(session["deadline_event_ms"])):
            continue
        if str(trade["side"]).upper() != "SELL" or float(trade["size"]) <= 0.0:
            continue
        capacity = float(trade["size"])
        matches = [
            index for index, leg in enumerate(legs)
            if str(leg["token"]) == str(trade["token"])
            and float(trade["price"]) <= float(leg["limit_price"]) + 1e-12
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


def mature_session(
    session: dict[str, Any],
    tape: list[dict[str, Any]],
    gamma: str,
    clob: str,
    slippage_bps: float,
    capital_cost_bps_per_hour: float,
) -> dict[str, Any] | None:
    descriptor = session.get("execution_descriptor")
    if not isinstance(descriptor, dict) or int(descriptor.get("descriptor_version") or 0) != 2:
        return None
    legs, filled, mask, full_mask = _simulate_fills(session, tape)
    if not legs:
        return None

    books, unwind_received = base.fetch_books(clob, [str(leg["token"]) for leg in legs])
    stress_pnl: dict[str, float | None] = {}
    for label, mult in STRESSES:
        if mask == full_mask:
            stress_pnl[label] = full_completion_stress_pnl(session, mult, capital_cost_bps_per_hour)
            continue
        pnl = 0.0
        valid = True
        entry_capital = 0.0
        for index, leg in enumerate(legs):
            shares = filled[index]
            if shares <= 1e-12:
                continue
            raw = base.fetch_market(gamma, str(leg["market_id"]))
            book = books.get(str(leg["token"]))
            if raw is None or book is None:
                valid = False
                break
            details = base.resolve_fee_details(raw, clob, str(raw.get("conditionId") or ""), str(leg["token"]))
            if not details.verified:
                valid = False
                break
            exit_price = base.sell_vwap(book, shares, slippage_bps * mult)
            if exit_price is None:
                valid = False
                break
            entry_price_cash = shares * float(leg["limit_price"])
            entry_fee_cash = shares * float(leg["entry_fee_per_share"])
            exit_fee_cash = shares * base.fee_per_share(exit_price, details, taker=True)
            entry_capital += entry_price_cash + entry_fee_cash
            exit_cash = shares * exit_price
            pnl += exit_cash - entry_price_cash - mult * (entry_fee_cash + exit_fee_cash)
        if valid and entry_capital > 0.0:
            rate = max(0.0, capital_cost_bps_per_hour) / 10000.0
            partial_hold_seconds = max(0.0, (int(session["deadline_received_ms"]) - int(session["origin_received_ms"])) / 1000.0)
            pnl -= mult * entry_capital * rate * partial_hold_seconds / 3600.0
        stress_pnl[label] = pnl if valid else None

    if any(value is None for value in stress_pnl.values()):
        return None
    max_notional = max(1e-12, float(base.finite(session.get("max_notional"), 0.0)))
    stress_return = {label: float(value) / max_notional for label, value in stress_pnl.items() if value is not None}
    return {
        "evidence_version": 2,
        "session_id": session["session_id"],
        "signature": session["signature"],
        "bundle_id": session["bundle_id"],
        "strategy": session["strategy"],
        "event_id": session["event_id"],
        "origin_received_ms": session["origin_received_ms"],
        "deadline_received_ms": session["deadline_received_ms"],
        "matured_ms": base.now_ms(),
        "unwind_book_received_ms": unwind_received,
        "state_mask": mask,
        "full_mask": full_mask,
        "full_completion": mask == full_mask,
        "filled_shares": filled,
        "required_flow": [float(leg["required_flow"]) for leg in legs],
        "stress_pnl": stress_pnl,
        "stress_return_on_notional": stress_return,
        "execution_descriptor": dict(descriptor),
    }


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
    if str(old.get("terminal_floor_reason") or "") != str(new.get("terminal_floor_reason") or ""):
        reasons.append("terminal_floor_contract")

    old_edge = float(base.finite(old.get("expected_edge"), math.inf))
    new_edge = float(base.finite(new.get("expected_edge"), -math.inf))
    if old_edge > new_edge + max(0.0, edge_tolerance):
        reasons.append("historical_edge_too_favorable")
    old_notional = max(0.0, float(base.finite(old.get("max_notional"), 0.0)))
    new_notional = max(0.0, float(base.finite(new.get("max_notional"), 0.0)))
    if old_notional + 1e-12 < (1.0 - max(0.0, min(0.25, size_tolerance_fraction))) * new_notional:
        reasons.append("historical_notional_too_small")

    old_legs = {str(row.get("key") or ""): row for row in old.get("legs", []) if isinstance(row, dict)}
    new_legs = {str(row.get("key") or ""): row for row in new.get("legs", []) if isinstance(row, dict)}
    if set(old_legs) != set(new_legs) or not new_legs:
        return False, reasons + ["leg_set"]
    size_mult = 1.0 - max(0.0, min(0.25, size_tolerance_fraction))
    burden_mult = 1.0 - max(0.0, min(0.25, burden_tolerance_fraction))
    for key in sorted(new_legs):
        hist, curr = old_legs[key], new_legs[key]
        if float(base.finite(hist.get("target_shares"), 0.0)) + 1e-12 < size_mult * float(base.finite(curr.get("target_shares"), 0.0)):
            reasons.append(f"historical_target_too_small:{key}")
        if float(base.finite(hist.get("required_flow_to_target"), 0.0)) + 1e-12 < burden_mult * float(base.finite(curr.get("required_flow_to_target"), 0.0)):
            reasons.append(f"historical_queue_burden_too_easy:{key}")
        h_ticks = float(base.finite(hist.get("quote_ticks_from_bid"), math.inf))
        c_ticks = float(base.finite(curr.get("quote_ticks_from_bid"), -math.inf))
        if not math.isfinite(h_ticks) or not math.isfinite(c_ticks) or abs(h_ticks - c_ticks) > max(0.0, quote_tick_tolerance):
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
    origins = sorted(int(row.get("origin_received_ms") or 0) for row in rows if int(row.get("origin_received_ms") or 0) > 0)
    spacings = [b - a for a, b in zip(origins, origins[1:]) if b > a]
    overlap = 1
    if spacings:
        median_spacing = statistics.median(spacings)
        overlap = max(1, math.ceil(max(1, int(window_seconds)) * 1000 / max(1.0, median_spacing)))
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


def evidence_for(
    current: dict[str, Any], completed: list[dict[str, Any]], min_sessions: int, reps: int, quantile: float
) -> dict[str, Any]:
    signature_value = str(current.get("signature") or "")
    structural = sorted(
        [row for row in completed if row.get("signature") == signature_value],
        key=lambda row: int(row.get("origin_received_ms") or 0),
    )[-300:]
    rows: list[dict[str, Any]] = []
    rejected = Counter()
    for row in structural:
        ok, reasons = comparable_session(row, current)
        if ok:
            rows.append(row)
        else:
            for reason in reasons:
                rejected[reason] += 1
    rows = rows[-150:]
    current_descriptor = current.get("execution_descriptor") if isinstance(current.get("execution_descriptor"), dict) else {}
    window_seconds = int(current_descriptor.get("window_seconds") or 0)
    effective = effective_nonoverlap_sessions(rows, window_seconds)
    block_length = dependence_block_length(rows, window_seconds)
    state_counts = Counter(int(row.get("state_mask") or 0) for row in rows)
    full_mask = (1 << len(current.get("legs", []))) - 1
    full = state_counts.get(full_mask, 0)
    result: dict[str, Any] = {
        "structural_sessions": len(structural),
        "comparable_sessions": len(rows),
        "effective_nonoverlap_sessions": effective,
        "dependence_block_length": block_length,
        "rejected_by_transport": dict(rejected),
        "joint_state_counts": {str(key): value for key, value in sorted(state_counts.items())},
        "joint_state_probabilities": {
            str(key): value / len(rows) for key, value in sorted(state_counts.items())
        } if rows else {},
        "full_completions": full,
        "full_completion_probability": full / len(rows) if rows else 0.0,
        "stress": {},
    }
    accepted = effective >= int(min_sessions) and full > 0
    current_notional = max(0.0, float(base.finite(current_descriptor.get("max_notional"), 0.0)))
    for label, _mult in STRESSES:
        values: list[float] = []
        for row in rows:
            stress_return = row.get("stress_return_on_notional")
            if not isinstance(stress_return, dict) or stress_return.get(label) is None:
                continue
            value = float(base.finite(stress_return.get(label), math.nan))
            if math.isfinite(value):
                values.append(value)
        lower_return = circular_block_bootstrap_lower(
            values,
            seed=20260826 + sum(ord(ch) for ch in signature_value + label),
            reps=reps,
            quantile=quantile,
            block_length=block_length,
        )
        mean_return = sum(values) / len(values) if values else -math.inf
        result["stress"][label] = {
            "n": len(values),
            "mean_return_on_notional": mean_return,
            "block_bootstrap_lower_return": lower_return,
            "transported_mean_pnl_current_notional": mean_return * current_notional if math.isfinite(mean_return) else -math.inf,
            "transported_lower_pnl_current_notional": lower_return * current_notional if math.isfinite(lower_return) else -math.inf,
        }
        accepted = accepted and len(values) == len(rows) and effective >= int(min_sessions) and lower_return > 0.0
    result["accepted"] = bool(accepted)
    return result


def main() -> int:
    parser = argparse.ArgumentParser(description="Executable, state-comparable, dependence-robust Graph/RV PAPER guard")
    parser.add_argument("--config", type=Path, required=True)
    parser.add_argument("--input", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--state", type=Path, required=True)
    parser.add_argument("--status", type=Path, required=True)
    parser.add_argument("--trade-tape", type=Path, required=True)
    parser.add_argument("--window-seconds", type=int, default=180)
    parser.add_argument("--min-sessions", type=int, default=4)
    parser.add_argument("--slippage-bps", type=float, default=5.0)
    parser.add_argument("--capital-cost-bps-per-hour", type=float, default=0.25)
    parser.add_argument("--bootstrap-reps", type=int, default=800)
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
        outcome = mature_session(
            session, tape, gamma, clob, args.slippage_bps, args.capital_cost_bps_per_hour
        )
        if outcome is None:
            invalid.append({
                "session_id": session.get("session_id"),
                "signature": session.get("signature"),
                "reason": "executable_pnl_or_fee_or_terminal_floor_unavailable",
                "timestamp": int(time.time()),
            })
        else:
            completed.append(outcome)
    completed = completed[-3000:]
    invalid = invalid[-750:]

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
            current, descriptor_reason = attach_execution_descriptor(current, gamma, clob, args.window_seconds)
            if current is None:
                reason = descriptor_reason
        decision: dict[str, Any] = {"bundle_id": bundle_id, "signature": sig, "snapshot": reason, "routed": False}
        if current is not None:
            evidence = evidence_for(current, completed, args.min_sessions, args.bootstrap_reps, args.bootstrap_quantile)
            decision["evidence"] = evidence
            if evidence["accepted"]:
                accepted_rows.extend(rows)
                decision["routed"] = True
            if sig not in open_signatures:
                open_sessions.append(current)
                open_signatures.add(sig)
                decision["registration"] = "registered_current_execution_state"
            else:
                decision["registration"] = "existing_open_session"
        else:
            decision["evidence"] = {"accepted": False, "reason": "current_snapshot_or_terminal_floor_unavailable"}
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
        "state_comparable_transport": True,
        "full_completion_uses_executed_entry_costs": True,
        "quoted_expected_edge_is_not_observed_pnl": True,
        "dependence_robust_block_bootstrap": True,
        "joint_state_ev_not_product_of_marginals": True,
        "input_bundles": len(grouped),
        "routed_bundles": len({row["bundle_id"] for row in accepted_rows}),
        "open_sessions": len(open_sessions),
        "completed_sessions": len(completed),
        "invalid_sessions": len(invalid),
        "minimum_effective_sessions": args.min_sessions,
        "decisions": decisions,
        "contracts": [
            "prospective_dual_clock_fill",
            "complete_nonaugmented_negrisk_terminal_floor_proof",
            "full_completion_payout_minus_executed_entry_costs",
            "partial_state_contemporaneous_depth_aware_unwind",
            "stress_entry_exit_fees_slippage_and_capital_time",
            "same_execution_state_transport",
            "chronological_circular_block_bootstrap",
            "overlapping_sessions_not_independent",
            "normalized_return_transport_to_current_notional",
            "positive_1x_1.5x_2x_lower_bound_before_route",
        ],
    }
    base.atomic_json(args.status, status)
    print(json.dumps(status, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
