#!/usr/bin/env python3
from __future__ import annotations

import argparse
import csv
import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any


TERMINAL_ACTIONS = {
    "FILL",
    "CANCEL_TTL",
    "CANCEL_STALE",
    "CANCEL_KILL",
    "CANCEL_GAME_START",
    "CANCEL_CAPITAL",
    "CANCEL_LEGACY_STATE",
}


def _f(value: Any, default: float = 0.0) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def _i(value: Any, default: int = 0) -> int:
    try:
        return int(float(value))
    except (TypeError, ValueError):
        return default


@dataclass
class OrderWindow:
    market_id: str
    token_id: str
    posted_ts: int
    limit_price: float
    shares: float
    queue_ahead: float
    end_ts: int
    terminal_action: str = "TTL_ASSUMED"


@dataclass
class TapeTrade:
    ts: int
    received_ms: int
    asset_id: str
    side: str
    price: float
    size: float


def read_orders(path: Path, ttl_seconds: int) -> list[OrderWindow]:
    windows: list[OrderWindow] = []
    active: dict[str, int] = {}
    if not path.exists():
        return windows
    with path.open(newline="", encoding="utf-8") as fh:
        for row in csv.DictReader(fh):
            action = (row.get("action") or "").upper()
            market = row.get("market_id") or ""
            ts = _i(row.get("timestamp"))
            if action == "POST":
                if market in active:
                    prior = windows[active[market]]
                    prior.end_ts = min(prior.end_ts, ts)
                    prior.terminal_action = "NEXT_POST"
                windows.append(
                    OrderWindow(
                        market_id=market,
                        token_id=row.get("token_id") or "",
                        posted_ts=ts,
                        limit_price=_f(row.get("limit_price")),
                        shares=max(0.0, _f(row.get("remaining_shares"))),
                        queue_ahead=max(0.0, _f(row.get("queue_ahead"))),
                        end_ts=ts + max(0, int(ttl_seconds)),
                    )
                )
                active[market] = len(windows) - 1
                continue
            if action in TERMINAL_ACTIONS and market in active:
                idx = active.pop(market)
                windows[idx].end_ts = min(windows[idx].end_ts, ts)
                windows[idx].terminal_action = action
    return windows


def read_tape(path: Path) -> list[TapeTrade]:
    rows: list[TapeTrade] = []
    if not path.exists():
        return rows
    with path.open(newline="", encoding="utf-8") as fh:
        for row in csv.DictReader(fh):
            trade = TapeTrade(
                ts=_i(row.get("timestamp")),
                received_ms=_i(row.get("received_ms")),
                asset_id=row.get("asset_id") or "",
                side=(row.get("side") or "").upper(),
                price=_f(row.get("price")),
                size=max(0.0, _f(row.get("size"))),
            )
            if trade.ts > 0 and trade.received_ms > 0 and trade.asset_id and trade.size > 0.0:
                rows.append(trade)
    rows.sort(key=lambda x: (x.ts, x.received_ms, x.asset_id, x.price, x.size))
    return rows


def replay_one(order: OrderWindow, trades: list[TapeTrade], *, require_received_before_end: bool) -> dict[str, float | int]:
    queue = max(0.0, order.queue_ahead)
    remaining = max(0.0, order.shares)
    eligible = 0
    used = 0
    first_fill_ts = 0
    filled = 0.0
    for trade in trades:
        if trade.asset_id != order.token_id:
            continue
        if trade.ts <= order.posted_ts or trade.ts > order.end_ts:
            continue
        if require_received_before_end and trade.received_ms > order.end_ts * 1000:
            continue
        if trade.side != "SELL" or trade.price > order.limit_price + 1e-9:
            continue
        eligible += 1
        flow = trade.size
        q = min(queue, flow)
        queue -= q
        flow -= q
        if flow <= 0.0 or remaining <= 0.0:
            continue
        this_fill = min(remaining, flow)
        remaining -= this_fill
        filled += this_fill
        used += 1
        if first_fill_ts == 0:
            first_fill_ts = trade.ts
        if remaining <= 1e-12:
            break
    return {
        "eligible_trades": eligible,
        "fill_trades": used,
        "filled_shares": filled,
        "remaining_shares": max(0.0, remaining),
        "remaining_queue": max(0.0, queue),
        "first_fill_ts": first_fill_ts,
    }


def summarize(order_log: Path, tape_path: Path, ttl_seconds: int) -> dict[str, Any]:
    orders = read_orders(order_log, ttl_seconds)
    trades = read_tape(tape_path)
    causal_fill_orders = 0
    event_fill_orders = 0
    causal_filled_shares = 0.0
    event_filled_shares = 0.0
    delayed_only_orders = 0
    details = []
    for order in orders:
        causal = replay_one(order, trades, require_received_before_end=True)
        event = replay_one(order, trades, require_received_before_end=False)
        cfill = float(causal["filled_shares"])
        efill = float(event["filled_shares"])
        causal_fill_orders += cfill > 1e-12
        event_fill_orders += efill > 1e-12
        causal_filled_shares += cfill
        event_filled_shares += efill
        delayed_only_orders += efill > 1e-12 and cfill <= 1e-12
        if cfill > 1e-12 or efill > 1e-12:
            details.append(
                {
                    "market_id": order.market_id,
                    "token_id": order.token_id,
                    "posted_ts": order.posted_ts,
                    "end_ts": order.end_ts,
                    "terminal_action": order.terminal_action,
                    "limit_price": order.limit_price,
                    "posted_shares": order.shares,
                    "initial_queue_ahead": order.queue_ahead,
                    "causal_received": causal,
                    "event_time_eventually_observed": event,
                }
            )
    return {
        "schema": "hf_maker_shared_tape_audit_v1",
        "paper_only": True,
        "authenticated_execution": False,
        "orders": len(orders),
        "tape_trades": len(trades),
        "causal_fill_orders": causal_fill_orders,
        "causal_filled_shares": causal_filled_shares,
        "event_time_fill_orders": event_fill_orders,
        "event_time_filled_shares": event_filled_shares,
        "delayed_only_fill_orders": delayed_only_orders,
        "details": details,
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--order-log", required=True)
    parser.add_argument("--trade-tape", required=True)
    parser.add_argument("--ttl-seconds", type=int, required=True)
    parser.add_argument("--output", required=True)
    args = parser.parse_args()
    payload = summarize(Path(args.order_log), Path(args.trade_tape), args.ttl_seconds)
    Path(args.output).write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    print(json.dumps(payload, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
