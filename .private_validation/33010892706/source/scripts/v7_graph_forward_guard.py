#!/usr/bin/env python3
from __future__ import annotations

import argparse
import csv
import json
import math
import os
import random
import time
from collections import Counter, defaultdict
from pathlib import Path
from typing import Any

from v6_market_common import fee_per_share, finite, parse_array, request_json, resolve_fee_details

FIELDS = [
    "bundle_id", "strategy", "event_id", "created_ts", "mode", "expected_edge",
    "max_notional", "market_id", "side", "weight", "limit_price",
    "execution_deadline_ts", "hold_deadline_ts",
]
SCHEMA = "v7_graph_forward_guard_v1"


def now_ms() -> int:
    return time.time_ns() // 1_000_000


def atomic_json(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + f".tmp.{os.getpid()}")
    tmp.write_text(json.dumps(value, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    os.replace(tmp, path)


def atomic_csv(path: Path, rows: list[dict[str, Any]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + f".tmp.{os.getpid()}")
    with tmp.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=FIELDS)
        writer.writeheader()
        writer.writerows([{key: row.get(key, "") for key in FIELDS} for row in rows])
    os.replace(tmp, path)


def read_csv(path: Path) -> list[dict[str, str]]:
    try:
        with path.open(newline="", encoding="utf-8") as handle:
            return [dict(row) for row in csv.DictReader(handle)]
    except OSError:
        return []


def read_state(path: Path) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        value = {}
    if not isinstance(value, dict) or value.get("schema") != SCHEMA:
        return {"schema": SCHEMA, "paper_only": True, "open": [], "completed": [], "invalid": []}
    for key in ("open", "completed", "invalid"):
        if not isinstance(value.get(key), list):
            value[key] = []
    return value


def side_token(raw: dict[str, Any], side: str) -> str:
    ids = [str(value) for value in parse_array(raw.get("clobTokenIds"))]
    outcomes = [str(value).strip().upper() for value in parse_array(raw.get("outcomes"))]
    for index, outcome in enumerate(outcomes[: len(ids)]):
        if outcome == side.upper():
            return ids[index]
    if len(ids) >= 2:
        return ids[0] if side.upper() == "YES" else ids[1]
    return ""


def market_event_id(raw: dict[str, Any]) -> str:
    events = raw.get("events")
    if isinstance(events, list) and events and isinstance(events[0], dict):
        event_id = str(events[0].get("id") or "")
        if event_id:
            return event_id
    return str(raw.get("eventId") or raw.get("event_id") or "")


def signature(rows: list[dict[str, str]]) -> str:
    head = rows[0]
    legs = sorted(f"{row.get('market_id','')}:{str(row.get('side') or '').upper()}" for row in rows)
    return "|".join([str(head.get("strategy") or ""), str(head.get("event_id") or ""), *legs])


def fetch_market(gamma: str, market_id: str) -> dict[str, Any] | None:
    try:
        value = request_json(f"{gamma.rstrip('/')}/markets/{market_id}")
    except Exception:
        return None
    return value if isinstance(value, dict) else None


def fetch_books(clob: str, tokens: list[str]) -> tuple[dict[str, dict[str, Any]], int]:
    output: dict[str, dict[str, Any]] = {}
    received = now_ms()
    for start in range(0, len(tokens), 80):
        try:
            root = request_json(clob.rstrip("/") + "/books", [{"token_id": token} for token in tokens[start:start + 80]])
        except Exception:
            continue
        received = now_ms()
        for raw in root if isinstance(root, list) else []:
            if not isinstance(raw, dict):
                continue
            token = str(raw.get("asset_id") or "")
            bids: list[tuple[float, float]] = []
            asks: list[tuple[float, float]] = []
            for key, values in (("bids", bids), ("asks", asks)):
                for level in raw.get(key, []):
                    if not isinstance(level, dict):
                        continue
                    price = finite(level.get("price")); size = max(0.0, finite(level.get("size"), 0.0))
                    if math.isfinite(price) and 0.0 < price < 1.0 and size > 0.0:
                        values.append((price, size))
            bids.sort(reverse=True); asks.sort()
            if token and bids and asks:
                output[token] = {
                    "bids": bids, "asks": asks,
                    "bid": bids[0][0], "ask": asks[0][0], "bid_size": bids[0][1],
                    "tick": max(1e-6, finite(raw.get("tick_size"), 0.01)),
                    "min_order": max(1.0, finite(raw.get("min_order_size"), 1.0)),
                }
    return output, received


def queue_at(book: dict[str, Any], price: float) -> float:
    tick = float(book["tick"])
    return sum(size for px, size in book["bids"] if abs(px - price) <= max(1e-9, 0.25 * tick))


def sell_vwap(book: dict[str, Any], shares: float, slippage_bps: float) -> float | None:
    remaining = max(0.0, shares); cash = 0.0; sold = 0.0
    for price, size in book["bids"]:
        quantity = min(remaining, size)
        cash += quantity * price; sold += quantity; remaining -= quantity
        if remaining <= 1e-9:
            break
    if sold + 1e-9 < shares or sold <= 0.0:
        return None
    return max(1e-6, (cash / sold) * (1.0 - max(0.0, slippage_bps) / 10000.0))


def bootstrap_lower(values: list[float], seed: int, reps: int, quantile: float) -> float:
    if not values:
        return -math.inf
    if len(values) < 8:
        return min(values)
    rng = random.Random(seed); n = len(values); means: list[float] = []
    for _ in range(max(100, reps)):
        means.append(sum(values[rng.randrange(n)] for _ in range(n)) / n)
    means.sort()
    index = min(len(means) - 1, max(0, int(max(0.0, min(0.49, quantile)) * (len(means) - 1))))
    return means[index]


def tape_rows(path: Path) -> list[dict[str, Any]]:
    output: list[dict[str, Any]] = []
    for row in read_csv(path):
        try:
            output.append({
                "event_ms": int(float(row["timestamp"]) * 1000),
                "received_ms": int(row.get("received_ms") or 0),
                "token": str(row["asset_id"]), "side": str(row["side"]).upper(),
                "price": float(row["price"]), "size": float(row["size"]),
            })
        except (KeyError, TypeError, ValueError):
            continue
    output.sort(key=lambda row: (row["received_ms"], row["event_ms"], row["token"]))
    return output


def snapshot_candidate(
    rows: list[dict[str, str]], gamma: str, clob: str, window_seconds: int
) -> tuple[dict[str, Any] | None, str]:
    head = rows[0]
    strategy = str(head.get("strategy") or "")
    event_id = str(head.get("event_id") or "")
    if strategy not in {"GRAPH_RV", "STRUCTURAL_TYPED"}:
        return None, "unsupported_strategy"
    raw_markets: list[dict[str, Any]] = []
    tokens: list[str] = []
    for row in rows:
        raw = fetch_market(gamma, str(row.get("market_id") or ""))
        if raw is None:
            return None, "market_fetch"
        if market_event_id(raw) != event_id:
            return None, "canonical_event_mismatch"
        token = side_token(raw, str(row.get("side") or ""))
        if not token or token in tokens:
            return None, "duplicate_or_missing_token"
        raw_markets.append(raw); tokens.append(token)
    books, received = fetch_books(clob, tokens)
    if any(token not in books for token in tokens):
        return None, "book_missing"
    weights = [max(0.0, finite(row.get("weight"), 0.0)) for row in rows]
    limits = [finite(row.get("limit_price"), books[token]["bid"]) for row, token in zip(rows, tokens)]
    fees = []
    capital_per_unit = 0.0
    for raw, token, weight, limit, book in zip(raw_markets, tokens, weights, limits, [books[token] for token in tokens]):
        if weight <= 0.0 or not math.isfinite(limit) or limit <= 0.0 or limit >= float(book["ask"]) - 1e-12:
            return None, "invalid_limit_or_weight"
        details = resolve_fee_details(raw, clob, str(raw.get("conditionId") or ""), token)
        if not details.verified:
            return None, "fee_unverified"
        fees.append(details)
        capital_per_unit += weight * (limit + fee_per_share(limit, details, taker=False))
    max_notional = max(0.0, finite(head.get("max_notional"), 0.0))
    if capital_per_unit <= 1e-12 or max_notional <= 0.0:
        return None, "invalid_notional"
    units = max_notional / capital_per_unit
    if any(units * weight + 1e-9 < float(books[token]["min_order"]) for weight, token in zip(weights, tokens)):
        return None, "min_order"
    origin_received = received
    legs = []
    for row, raw, token, weight, limit, details in zip(rows, raw_markets, tokens, weights, limits, fees):
        target = units * weight
        queue = queue_at(books[token], limit)
        legs.append({
            "market_id": str(row.get("market_id") or ""), "side": str(row.get("side") or "").upper(),
            "token": token, "weight": weight, "limit_price": limit, "target_shares": target,
            "queue_ahead": queue, "required_flow": queue + target,
            "entry_fee_per_share": fee_per_share(limit, details, taker=False),
        })
    expected_edge = finite(head.get("expected_edge"), 0.0)
    return {
        "session_id": f"{signature(rows)}@{origin_received}",
        "signature": signature(rows), "bundle_id": str(head.get("bundle_id") or ""),
        "strategy": strategy, "event_id": event_id, "created_ts": int(finite(head.get("created_ts"), int(time.time()))),
        "origin_received_ms": origin_received, "origin_event_ms": origin_received,
        "deadline_received_ms": origin_received + int(window_seconds) * 1000,
        "deadline_event_ms": origin_received + int(window_seconds) * 1000,
        "expected_edge": expected_edge, "max_notional": max_notional, "capital_per_unit": capital_per_unit,
        "units": units, "legs": legs, "intent_rows": rows,
    }, "registered"


def mature_session(session: dict[str, Any], tape: list[dict[str, Any]], gamma: str, clob: str, slippage_bps: float) -> dict[str, Any] | None:
    legs = [dict(leg) for leg in session.get("legs", []) if isinstance(leg, dict)]
    if not legs:
        return None
    queues = [max(0.0, finite(leg.get("queue_ahead"), 0.0)) for leg in legs]
    remaining = [max(0.0, finite(leg.get("target_shares"), 0.0)) for leg in legs]
    filled = [0.0] * len(legs)
    for trade in tape:
        if not (int(session["origin_received_ms"]) < trade["received_ms"] <= int(session["deadline_received_ms"])):
            continue
        if not (int(session["origin_event_ms"]) < trade["event_ms"] <= int(session["deadline_event_ms"])):
            continue
        if trade["side"] != "SELL" or trade["size"] <= 0.0:
            continue
        capacity = float(trade["size"])
        matches = [
            index for index, leg in enumerate(legs)
            if leg["token"] == trade["token"] and trade["price"] <= float(leg["limit_price"]) + 1e-12 and remaining[index] > 1e-12
        ]
        for index in matches:
            if capacity <= 1e-12:
                break
            used = min(queues[index], capacity); queues[index] -= used; capacity -= used
            own = min(remaining[index], capacity); remaining[index] -= own; filled[index] += own; capacity -= own
    mask = 0
    for index, leg in enumerate(legs):
        if remaining[index] <= 1e-9:
            mask |= 1 << index
    full_mask = (1 << len(legs)) - 1
    current_books, unwind_received = fetch_books(clob, [str(leg["token"]) for leg in legs])
    stress_pnl: dict[str, float | None] = {}
    for mult in (1.0, 1.5, 2.0):
        if mask == full_mask:
            profit = float(session["units"]) * float(session["expected_edge"])
            stress_pnl[f"{mult:g}x"] = profit
            continue
        pnl = 0.0; valid = True
        for index, leg in enumerate(legs):
            shares = filled[index]
            if shares <= 1e-12:
                continue
            raw = fetch_market(gamma, str(leg["market_id"]))
            book = current_books.get(str(leg["token"]))
            if raw is None or book is None:
                valid = False; break
            details = resolve_fee_details(raw, clob, str(raw.get("conditionId") or ""), str(leg["token"]))
            if not details.verified:
                valid = False; break
            exit_price = sell_vwap(book, shares, slippage_bps * mult)
            if exit_price is None:
                valid = False; break
            entry_cost = shares * (float(leg["limit_price"]) + float(leg["entry_fee_per_share"]))
            exit_cash = shares * (exit_price - fee_per_share(exit_price, details, taker=True))
            pnl += exit_cash - entry_cost
        stress_pnl[f"{mult:g}x"] = pnl if valid else None
    if any(value is None for value in stress_pnl.values()):
        return None
    return {
        "session_id": session["session_id"], "signature": session["signature"], "bundle_id": session["bundle_id"],
        "strategy": session["strategy"], "event_id": session["event_id"], "origin_received_ms": session["origin_received_ms"],
        "deadline_received_ms": session["deadline_received_ms"], "matured_ms": now_ms(), "unwind_book_received_ms": unwind_received,
        "state_mask": mask, "full_mask": full_mask, "full_completion": mask == full_mask,
        "filled_shares": filled, "required_flow": [float(leg["required_flow"]) for leg in legs],
        "stress_pnl": stress_pnl,
    }


def evidence_for(signature_value: str, completed: list[dict[str, Any]], min_sessions: int, reps: int, quantile: float) -> dict[str, Any]:
    rows = [row for row in completed if row.get("signature") == signature_value]
    rows = rows[-100:]
    result: dict[str, Any] = {"sessions": len(rows), "full_completions": sum(bool(row.get("full_completion")) for row in rows)}
    result["full_completion_probability"] = result["full_completions"] / len(rows) if rows else 0.0
    result["stress"] = {}
    accepted = len(rows) >= min_sessions and result["full_completions"] > 0
    for mult in ("1x", "1.5x", "2x"):
        values = [float(row["stress_pnl"][mult]) for row in rows if row.get("stress_pnl", {}).get(mult) is not None]
        lower = bootstrap_lower(values, 20260826 + sum(ord(ch) for ch in signature_value + mult), reps, quantile)
        mean = sum(values) / len(values) if values else -math.inf
        result["stress"][mult] = {"mean_pnl": mean, "bootstrap_lower": lower, "n": len(values)}
        accepted = accepted and len(values) >= min_sessions and lower > 0.0
    result["accepted"] = accepted
    return result


def main() -> int:
    parser = argparse.ArgumentParser(description="Prospective point-in-time Graph/RV joint-state PAPER admission guard")
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
    state = read_state(args.state); current_ms = now_ms(); tape = tape_rows(args.trade_tape)
    open_sessions: list[dict[str, Any]] = []
    completed = [dict(row) for row in state["completed"] if isinstance(row, dict)]
    invalid = [dict(row) for row in state["invalid"] if isinstance(row, dict)]
    for session in [dict(row) for row in state["open"] if isinstance(row, dict)]:
        if current_ms < int(session.get("deadline_received_ms") or 0):
            open_sessions.append(session); continue
        outcome = mature_session(session, tape, gamma, clob, args.slippage_bps)
        if outcome is None:
            invalid.append({"session_id": session.get("session_id"), "signature": session.get("signature"), "reason": "unwind_book_or_fee_unavailable", "timestamp": int(time.time())})
        else:
            completed.append(outcome)
    completed = completed[-2000:]; invalid = invalid[-500:]

    grouped: dict[str, list[dict[str, str]]] = defaultdict(list)
    for row in read_csv(args.input):
        bundle_id = str(row.get("bundle_id") or "")
        if bundle_id:
            grouped[bundle_id].append(row)
    accepted_rows: list[dict[str, Any]] = []
    decisions: list[dict[str, Any]] = []
    open_signatures = {str(session.get("signature") or "") for session in open_sessions}
    for bundle_id, rows in grouped.items():
        sig = signature(rows)
        evidence = evidence_for(sig, completed, args.min_sessions, args.bootstrap_reps, args.bootstrap_quantile)
        decision = {"bundle_id": bundle_id, "signature": sig, "evidence": evidence, "routed": False}
        if evidence["accepted"]:
            accepted_rows.extend(rows); decision["routed"] = True
        if sig not in open_signatures:
            session, reason = snapshot_candidate(rows, gamma, clob, args.window_seconds)
            decision["registration"] = reason
            if session is not None:
                open_sessions.append(session); open_signatures.add(sig)
        else:
            decision["registration"] = "existing_open_session"
        decisions.append(decision)

    new_state = {
        "schema": SCHEMA, "paper_only": True, "authenticated_execution": False, "updated_ms": current_ms,
        "open": open_sessions, "completed": completed, "invalid": invalid,
    }
    atomic_json(args.state, new_state); atomic_csv(args.output, accepted_rows)
    status = {
        "schema": SCHEMA, "timestamp": int(time.time()), "paper_only": True, "authenticated_execution": False,
        "prospective_only": True, "point_in_time_queue_snapshot": True, "dual_clock_forward_fill": True,
        "input_bundles": len(grouped), "routed_bundles": len({row["bundle_id"] for row in accepted_rows}),
        "open_sessions": len(open_sessions), "completed_sessions": len(completed), "invalid_sessions": len(invalid),
        "minimum_sessions_per_signature": args.min_sessions, "decisions": decisions,
        "contracts": ["no_current_book_historical_replay", "received_ms_availability", "event_time_spacing", "same_window_joint_state", "partial_state_contemporaneous_unwind", "cost_stress_1x_1.5x_2x", "positive_bootstrap_lower_before_route"],
    }
    atomic_json(args.status, status); print(json.dumps(status, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
