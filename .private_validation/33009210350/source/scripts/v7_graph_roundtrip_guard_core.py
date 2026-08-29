#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import math
import time
from collections import defaultdict
from pathlib import Path
from typing import Any

import v7_graph_execution_guard as v2

SCHEMA = "v7_graph_roundtrip_guard_v3"
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


def attach_roundtrip_descriptor(
    session: dict[str, Any], clob: str, window_seconds: int
) -> tuple[dict[str, Any] | None, str]:
    """Freeze the state needed to transport executable fixed-horizon evidence.

    No terminal payout floor is assumed. Even a fully completed NegRisk basket is
    judged by the cash that could be realized by liquidating all filled legs at
    the evidence horizon against contemporaneous order-book depth.
    """
    legs = [dict(leg) for leg in session.get("legs", []) if isinstance(leg, dict)]
    if not legs:
        return None, "descriptor_no_legs"
    tokens = [str(leg.get("token") or "") for leg in legs]
    books, descriptor_received_ms = v2.base.fetch_books(clob, tokens)
    if any(token not in books for token in tokens):
        return None, "descriptor_book_missing"

    descriptor_legs: list[dict[str, Any]] = []
    for leg in legs:
        token = str(leg["token"])
        book = books[token]
        target = max(0.0, float(v2.base.finite(leg.get("target_shares"), 0.0)))
        queue = max(0.0, float(v2.base.finite(leg.get("queue_ahead"), 0.0)))
        required = max(0.0, float(v2.base.finite(leg.get("required_flow"), queue + target)))
        tick = max(1e-9, float(v2.base.finite(book.get("tick"), 0.01)))
        bid = float(v2.base.finite(book.get("bid")))
        limit = float(v2.base.finite(leg.get("limit_price")))
        entry_fee = max(0.0, float(v2.base.finite(leg.get("entry_fee_per_share"), math.nan)))
        if target <= 0.0 or not math.isfinite(bid) or not math.isfinite(limit) or not math.isfinite(entry_fee):
            return None, "descriptor_invalid_leg"
        descriptor_legs.append({
            "key": _leg_key(leg),
            "target_shares": target,
            "queue_ahead": queue,
            "required_flow": required,
            "required_flow_to_target": required / max(target, 1e-12),
            "queue_to_target": queue / max(target, 1e-12),
            "quote_ticks_from_bid": (limit - bid) / tick,
            "entry_fee_per_share": entry_fee,
        })
    descriptor_legs.sort(key=lambda row: row["key"])

    max_notional = max(0.0, float(v2.base.finite(session.get("max_notional"), 0.0)))
    if max_notional <= 0.0:
        return None, "descriptor_invalid_notional"
    enriched = dict(session)
    enriched["execution_descriptor"] = {
        "descriptor_version": 3,
        "descriptor_received_ms": descriptor_received_ms,
        "window_seconds": int(window_seconds),
        "quote_policy": "maker_limit_ticks_from_bid",
        "pnl_contract": "fixed_horizon_depth_aware_roundtrip_liquidation",
        "terminal_floor_reason": "not_used",
        "expected_edge": float(v2.base.finite(session.get("expected_edge"), 0.0)),
        "max_notional": max_notional,
        "legs": descriptor_legs,
    }
    return enriched, "roundtrip_descriptor_registered"


def roundtrip_pnl_components(
    *,
    entry_price_cash: float,
    entry_fee_cash: float,
    exit_cash_before_fee: float,
    exit_fee_cash: float,
    capital_time_cash: float,
    cost_multiplier: float,
) -> float:
    """Pure executable round-trip accounting used for both full and partial states."""
    mult = max(0.0, float(cost_multiplier))
    return (
        float(exit_cash_before_fee)
        - float(entry_price_cash)
        - mult * (float(entry_fee_cash) + float(exit_fee_cash) + float(capital_time_cash))
    )


def mature_session(
    session: dict[str, Any],
    tape: list[dict[str, Any]],
    gamma: str,
    clob: str,
    slippage_bps: float,
    capital_cost_bps_per_hour: float,
) -> dict[str, Any] | None:
    descriptor = session.get("execution_descriptor")
    if not isinstance(descriptor, dict) or int(descriptor.get("descriptor_version") or 0) != 3:
        return None
    legs, filled, mask, full_mask = v2._simulate_fills(session, tape)
    if not legs:
        return None

    books, liquidation_received_ms = v2.base.fetch_books(clob, [str(leg["token"]) for leg in legs])
    rate_per_hour = max(0.0, float(capital_cost_bps_per_hour)) / 10000.0
    horizon_seconds = max(
        0.0,
        (int(session["deadline_received_ms"]) - int(session["origin_received_ms"])) / 1000.0,
    )
    stress_pnl: dict[str, float | None] = {}
    for label, mult in STRESSES:
        pnl = 0.0
        valid = True
        for index, leg in enumerate(legs):
            shares = filled[index]
            if shares <= 1e-12:
                continue
            raw = v2.base.fetch_market(gamma, str(leg["market_id"]))
            book = books.get(str(leg["token"]))
            if raw is None or book is None:
                valid = False
                break
            details = v2.base.resolve_fee_details(
                raw, clob, str(raw.get("conditionId") or ""), str(leg["token"])
            )
            if not details.verified:
                valid = False
                break
            # Slippage stress changes the actually executable liquidation price.
            exit_price = v2.base.sell_vwap(book, shares, slippage_bps * mult)
            if exit_price is None:
                valid = False
                break
            entry_price_cash = shares * float(leg["limit_price"])
            entry_fee_cash = shares * float(leg["entry_fee_per_share"])
            exit_cash_before_fee = shares * exit_price
            exit_fee_cash = shares * v2.base.fee_per_share(exit_price, details, taker=True)
            capital_time_cash = (
                (entry_price_cash + entry_fee_cash)
                * rate_per_hour
                * horizon_seconds
                / 3600.0
            )
            pnl += roundtrip_pnl_components(
                entry_price_cash=entry_price_cash,
                entry_fee_cash=entry_fee_cash,
                exit_cash_before_fee=exit_cash_before_fee,
                exit_fee_cash=exit_fee_cash,
                capital_time_cash=capital_time_cash,
                cost_multiplier=mult,
            )
        stress_pnl[label] = pnl if valid else None

    if any(value is None for value in stress_pnl.values()):
        return None
    max_notional = max(1e-12, float(v2.base.finite(session.get("max_notional"), 0.0)))
    stress_return = {
        label: float(value) / max_notional
        for label, value in stress_pnl.items()
        if value is not None
    }
    return {
        "evidence_version": 3,
        "session_id": session["session_id"],
        "signature": session["signature"],
        "bundle_id": session["bundle_id"],
        "strategy": session["strategy"],
        "event_id": session["event_id"],
        "origin_received_ms": session["origin_received_ms"],
        "deadline_received_ms": session["deadline_received_ms"],
        "matured_ms": v2.base.now_ms(),
        "liquidation_book_received_ms": liquidation_received_ms,
        "state_mask": mask,
        "full_mask": full_mask,
        "full_completion": mask == full_mask,
        "filled_shares": filled,
        "required_flow": [float(leg["required_flow"]) for leg in legs],
        "stress_pnl": stress_pnl,
        "stress_return_on_notional": stress_return,
        "execution_descriptor": dict(descriptor),
        "pnl_contract": "fixed_horizon_depth_aware_roundtrip_liquidation",
    }


def comparable_session(
    historical: dict[str, Any], current: dict[str, Any]
) -> tuple[bool, list[str]]:
    if int(historical.get("evidence_version") or 0) != 3:
        return False, ["legacy_evidence_schema"]
    old = historical.get("execution_descriptor")
    new = current.get("execution_descriptor")
    if not isinstance(old, dict) or not isinstance(new, dict):
        return False, ["descriptor_missing"]
    if old.get("pnl_contract") != "fixed_horizon_depth_aware_roundtrip_liquidation":
        return False, ["historical_pnl_contract"]
    if new.get("pnl_contract") != "fixed_horizon_depth_aware_roundtrip_liquidation":
        return False, ["current_pnl_contract"]
    # Reuse V2's conservative execution-state dominance checks by mapping both
    # descriptor versions to its expected schema. No terminal floor is used.
    hist = dict(historical)
    curr = dict(current)
    hist["evidence_version"] = 2
    old2 = dict(old); old2["descriptor_version"] = 2; old2["terminal_floor_reason"] = "not_used"
    new2 = dict(new); new2["descriptor_version"] = 2; new2["terminal_floor_reason"] = "not_used"
    hist["execution_descriptor"] = old2
    curr["execution_descriptor"] = new2
    return v2.comparable_session(hist, curr)


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
    descriptor = current.get("execution_descriptor") if isinstance(current.get("execution_descriptor"), dict) else {}
    window_seconds = int(descriptor.get("window_seconds") or 0)
    effective = v2.effective_nonoverlap_sessions(rows, window_seconds)
    block_length = v2.dependence_block_length(rows, window_seconds)
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
    current_notional = max(0.0, float(v2.base.finite(descriptor.get("max_notional"), 0.0)))
    for label, _mult in STRESSES:
        values = [
            float(row["stress_return_on_notional"][label])
            for row in rows
            if isinstance(row.get("stress_return_on_notional"), dict)
            and row["stress_return_on_notional"].get(label) is not None
            and math.isfinite(float(row["stress_return_on_notional"][label]))
        ]
        lower = v2.circular_block_bootstrap_lower(
            values,
            seed=20260826 + sum(ord(ch) for ch in signature_value + label),
            reps=reps,
            quantile=quantile,
            block_length=block_length,
        )
        mean = sum(values) / len(values) if values else -math.inf
        result["stress"][label] = {
            "n": len(values),
            "mean_return_on_notional": mean,
            "block_bootstrap_lower_return": lower,
            "transported_mean_pnl_current_notional": mean * current_notional if math.isfinite(mean) else -math.inf,
            "transported_lower_pnl_current_notional": lower * current_notional if math.isfinite(lower) else -math.inf,
        }
        accepted = accepted and len(values) == len(rows) and lower > 0.0
    result["accepted"] = bool(accepted)
    return result


def main() -> int:
    parser = argparse.ArgumentParser(description="V7 Graph/RV fixed-horizon executable round-trip guard")
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
    current_ms = v2.base.now_ms()
    tape = v2.base.tape_rows(args.trade_tape)
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
                "reason": "roundtrip_liquidation_or_fee_unavailable",
                "timestamp": int(time.time()),
            })
        else:
            completed.append(outcome)
    completed = completed[-3000:]
    invalid = invalid[-750:]

    grouped: dict[str, list[dict[str, str]]] = defaultdict(list)
    for row in v2.base.read_csv(args.input):
        bundle_id = str(row.get("bundle_id") or "")
        if bundle_id:
            grouped[bundle_id].append(row)

    accepted_rows: list[dict[str, Any]] = []
    decisions: list[dict[str, Any]] = []
    open_signatures = {str(session.get("signature") or "") for session in open_sessions}
    for bundle_id, rows in grouped.items():
        sig = v2.base.signature(rows)
        current, reason = v2.base.snapshot_candidate(rows, gamma, clob, args.window_seconds)
        if current is not None:
            current, descriptor_reason = attach_roundtrip_descriptor(current, clob, args.window_seconds)
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
                decision["registration"] = "registered_current_roundtrip_state"
            else:
                decision["registration"] = "existing_open_session"
        else:
            decision["evidence"] = {"accepted": False, "reason": "current_snapshot_unavailable"}
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
    v2.base.atomic_json(args.state, new_state)
    v2.base.atomic_csv(args.output, accepted_rows)
    status = {
        "schema": SCHEMA,
        "timestamp": int(time.time()),
        "paper_only": True,
        "authenticated_execution": False,
        "prospective_only": True,
        "pnl_contract": "fixed_horizon_depth_aware_roundtrip_liquidation",
        "terminal_payout_floor_assumed": False,
        "neg_risk_exactly_one_yes_assumed": False,
        "full_and_partial_states_share_executable_liquidation_accounting": True,
        "state_comparable_transport": True,
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
            "full_completion_is_not_quoted_expected_edge",
            "no_unproved_terminal_floor",
            "all_fill_states_depth_aware_horizon_liquidation",
            "stress_entry_exit_fees_slippage_and_capital_time",
            "same_execution_state_transport",
            "chronological_circular_block_bootstrap",
            "overlapping_sessions_not_independent",
            "normalized_return_transport_to_current_notional",
            "positive_1x_1.5x_2x_lower_bound_before_route",
        ],
    }
    v2.base.atomic_json(args.status, status)
    print(json.dumps(status, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
