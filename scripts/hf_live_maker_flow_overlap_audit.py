#!/usr/bin/env python3
from __future__ import annotations

import argparse
import csv
import json
from collections import defaultdict
from pathlib import Path
from typing import Any


def _float(value: Any, default: float = 0.0) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def _event_seconds(value: Any) -> float:
    ts = _float(value)
    if ts > 10_000_000_000:
        ts /= 1000.0
    return ts


def _received_seconds(row: dict[str, str]) -> float:
    if row.get("received_ms"):
        return _float(row["received_ms"]) / 1000.0
    if row.get("received_ts"):
        return _event_seconds(row["received_ts"])
    return _event_seconds(row.get("timestamp", 0))


def _read_csv(path: str | Path) -> list[dict[str, str]]:
    with Path(path).open(newline="", encoding="utf-8") as handle:
        return list(csv.DictReader(handle))


def _token(row: dict[str, str], field: str) -> str:
    return str(row.get(field, "")).strip()


def causal_recent_trades(
    trades: list[dict[str, str]],
    decision_ts: float,
    lookback_seconds: float,
) -> list[dict[str, str]]:
    lower = decision_ts - lookback_seconds
    out: list[dict[str, str]] = []
    for trade in trades:
        event_ts = _event_seconds(trade.get("timestamp"))
        received_ts = _received_seconds(trade)
        if lower <= event_ts <= decision_ts and received_ts <= decision_ts:
            out.append(trade)
    return out


def compatible_sell(
    trade: dict[str, str],
    limit_price: float,
) -> bool:
    return (
        str(trade.get("side", "")).upper() == "SELL"
        and _float(trade.get("price"), float("inf")) <= limit_price + 1e-12
    )


def audit(
    orders: list[dict[str, str]],
    order_log: list[dict[str, str]],
    tape: list[dict[str, str]],
    *,
    lookback_seconds: float = 900.0,
    min_tape_rows: int = 1,
) -> dict[str, Any]:
    tape_by_token: dict[str, list[dict[str, str]]] = defaultdict(list)
    for trade in tape:
        token = _token(trade, "asset_id") or _token(trade, "token_id")
        if token:
            tape_by_token[token].append(trade)

    log_timestamps = [_event_seconds(row.get("timestamp")) for row in order_log if row.get("timestamp")]
    first_tick = min(log_timestamps) if log_timestamps else None
    first_tick_rows = [
        row for row in order_log
        if first_tick is not None and abs(_event_seconds(row.get("timestamp")) - first_tick) < 1e-9
        and str(row.get("action", "")).upper() in {"POST", "SKIP_QUEUE"}
    ]

    order_details: list[dict[str, Any]] = []
    reserved = 0.0
    resting_with_any_tape = 0
    resting_with_causal_recent = 0
    resting_with_compatible_sell = 0
    for order in orders:
        token = _token(order, "token_id")
        decision_ts = _event_seconds(order.get("created_ts"))
        limit_price = _float(order.get("limit_price"))
        remaining = _float(order.get("remaining_shares"))
        token_trades = tape_by_token.get(token, [])
        recent = causal_recent_trades(token_trades, decision_ts, lookback_seconds)
        sells = [trade for trade in recent if compatible_sell(trade, limit_price)]
        notional = remaining * limit_price
        reserved += notional
        resting_with_any_tape += int(bool(token_trades))
        resting_with_causal_recent += int(bool(recent))
        resting_with_compatible_sell += int(bool(sells))
        order_details.append({
            "market_id": order.get("market_id", ""),
            "slug": order.get("slug", ""),
            "side": order.get("side", ""),
            "token_id": token,
            "limit_price": limit_price,
            "remaining_shares": remaining,
            "queue_ahead": _float(order.get("queue_ahead")),
            "created_ts": decision_ts,
            "reserved_notional_usd": notional,
            "same_token_trade_rows": len(token_trades),
            "causal_recent_same_token_rows": len(recent),
            "compatible_sell_rows_pre_decision": len(sells),
            "compatible_sell_volume_pre_decision": sum(_float(t.get("size")) for t in sells),
        })

    signal_tokens = {_token(row, "token_id") for row in first_tick_rows if _token(row, "token_id")}
    any_tape_signal_tokens = {token for token in signal_tokens if tape_by_token.get(token)}
    causal_signal_tokens: set[str] = set()
    for row in first_tick_rows:
        token = _token(row, "token_id")
        if not token or first_tick is None:
            continue
        if causal_recent_trades(tape_by_token.get(token, []), first_tick, lookback_seconds):
            causal_signal_tokens.add(token)

    tape_healthy_enough = len(tape) >= min_tape_rows and bool(tape_by_token)
    if not tape_healthy_enough:
        state = "INCONCLUSIVE_TAPE"
    elif orders and resting_with_causal_recent == 0:
        state = "STATIC_MAKER_ACTIVITY_MISMATCH"
    else:
        state = "ACTIVITY_PRESENT"

    return {
        "schema": "hf_live_maker_flow_overlap_audit_v1",
        "state": state,
        "lookback_seconds": lookback_seconds,
        "tape": {
            "rows": len(tape),
            "unique_tokens": len(tape_by_token),
            "healthy_enough_for_overlap_audit": tape_healthy_enough,
        },
        "first_tick": {
            "decision_ts": first_tick,
            "signal_rows": len(first_tick_rows),
            "signal_tokens": len(signal_tokens),
            "signal_tokens_with_any_tape_trade": len(any_tape_signal_tokens),
            "signal_tokens_with_causal_recent_trade": len(causal_signal_tokens),
            "post_rows": sum(str(row.get("action", "")).upper() == "POST" for row in first_tick_rows),
            "queue_skipped_rows": sum(str(row.get("action", "")).upper() == "SKIP_QUEUE" for row in first_tick_rows),
        },
        "resting": {
            "orders": len(orders),
            "reserved_notional_usd": reserved,
            "orders_with_any_tape_trade": resting_with_any_tape,
            "orders_with_causal_recent_trade": resting_with_causal_recent,
            "orders_with_compatible_sell": resting_with_compatible_sell,
        },
        "orders": order_details,
        "interpretation": (
            "Static executable-edge selection is reserving paper capital on tokens with no "
            "causal recent same-token activity in an otherwise non-empty public tape. "
            "Seed/rank HF maker candidates by causal recent activity before queue/fill and toxicity."
            if state == "STATIC_MAKER_ACTIVITY_MISMATCH"
            else "No static-maker activity mismatch is established by this sample."
        ),
    }


def main() -> int:
    parser = argparse.ArgumentParser(description="Audit live Micro Maker order/tape activity overlap")
    parser.add_argument("--orders", required=True)
    parser.add_argument("--order-log", required=True)
    parser.add_argument("--trade-tape", required=True)
    parser.add_argument("--lookback-seconds", type=float, default=900.0)
    parser.add_argument("--min-tape-rows", type=int, default=1)
    parser.add_argument("--output")
    args = parser.parse_args()

    report = audit(
        _read_csv(args.orders),
        _read_csv(args.order_log),
        _read_csv(args.trade_tape),
        lookback_seconds=args.lookback_seconds,
        min_tape_rows=args.min_tape_rows,
    )
    rendered = json.dumps(report, indent=2, sort_keys=True) + "\n"
    if args.output:
        Path(args.output).write_text(rendered, encoding="utf-8")
    print(rendered, end="")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
