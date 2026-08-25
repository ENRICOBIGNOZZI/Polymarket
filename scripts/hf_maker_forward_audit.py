#!/usr/bin/env python3
from __future__ import annotations

import argparse
import csv
import json
import math
from collections import defaultdict, deque
from pathlib import Path
from typing import Any


def finite(value: Any, default: float = math.nan) -> float:
    try:
        x = float(value)
    except (TypeError, ValueError):
        return default
    return x if math.isfinite(x) else default


def read_rows(path: Path) -> list[dict[str, str]]:
    if not path.exists() or path.stat().st_size == 0:
        return []
    with path.open(newline="", encoding="utf-8") as handle:
        return [dict(row) for row in csv.DictReader(handle)]


def _flow(
    tape: list[dict[str, str]], *, token: str, limit_price: float,
    receive_lo_ms: int | None = None, receive_hi_ms: int | None = None,
    event_lo_s: int | None = None, event_hi_s: int | None = None,
) -> tuple[int, float]:
    count = 0
    shares = 0.0
    for trade in tape:
        if str(trade.get("asset_id") or trade.get("token_id") or "") != token:
            continue
        if str(trade.get("side") or "").upper() != "SELL":
            continue
        price = finite(trade.get("price"))
        size = max(0.0, finite(trade.get("size"), 0.0))
        if not math.isfinite(price) or price > limit_price + 1e-12 or size <= 0.0:
            continue
        received_ms = int(finite(trade.get("received_ms"), 0.0))
        event_ts = int(finite(trade.get("timestamp"), 0.0))
        if receive_lo_ms is not None and received_ms < receive_lo_ms:
            continue
        if receive_hi_ms is not None and received_ms > receive_hi_ms:
            continue
        if event_lo_s is not None and event_ts <= event_lo_s:
            continue
        if event_hi_s is not None and event_ts > event_hi_s:
            continue
        count += 1
        shares += size
    return count, shares


def realized_roundtrip_pnl(fill_rows: list[dict[str, str]]) -> dict[str, Any]:
    inventory: dict[str, deque[list[float]]] = defaultdict(deque)
    buy_shares = 0.0
    sell_shares = 0.0
    realized = 0.0
    buys = 0
    sells = 0
    unmatched_sell_shares = 0.0

    rows = sorted(fill_rows, key=lambda row: int(finite(row.get("timestamp"), 0.0)))
    for row in rows:
        market = str(row.get("market_id") or "")
        action = str(row.get("action") or "").upper()
        shares = max(0.0, finite(row.get("shares"), 0.0))
        price = finite(row.get("price"), 0.0)
        fee = max(0.0, finite(row.get("fee"), 0.0))
        if shares <= 0 or price <= 0:
            continue
        if action.startswith("BUY_MAKER"):
            buys += 1
            buy_shares += shares
            unit_cost = (shares * price + fee) / shares
            inventory[market].append([shares, unit_cost])
            continue
        if not action.startswith("SELL_TAKER"):
            continue
        sells += 1
        sell_shares += shares
        remaining = shares
        proceeds_per_share = (shares * price - fee) / shares
        while remaining > 1e-12 and inventory[market]:
            lot = inventory[market][0]
            matched = min(remaining, lot[0])
            realized += matched * (proceeds_per_share - lot[1])
            remaining -= matched
            lot[0] -= matched
            if lot[0] <= 1e-12:
                inventory[market].popleft()
        unmatched_sell_shares += remaining

    open_shares = sum(lot[0] for q in inventory.values() for lot in q)
    return {
        "maker_buy_events": buys,
        "maker_buy_shares": buy_shares,
        "taker_exit_events": sells,
        "taker_exit_shares": sell_shares,
        "realized_closed_pnl": realized,
        "open_maker_shares": open_shares,
        "unmatched_sell_shares": unmatched_sell_shares,
    }


def audit(
    order_log: Path,
    trade_tape: Path,
    fills: Path,
    *,
    prior_lookback_seconds: int = 120,
    forward_horizon_seconds: int = 60,
    min_edge: float = 0.00005,
) -> dict[str, Any]:
    orders = read_rows(order_log)
    tape = read_rows(trade_tape)
    fill_rows = read_rows(fills)
    posts = [row for row in orders if str(row.get("action") or "").upper() == "POST"]

    audited: list[dict[str, Any]] = []
    flow_active = 0
    projected_fill_positive = 0
    future_compatible_positive = 0
    for post in posts:
        post_ts = int(finite(post.get("timestamp"), 0.0))
        token = str(post.get("token_id") or "")
        limit_price = finite(post.get("limit_price"), 0.0)
        own = max(0.0, finite(post.get("remaining_shares"), 0.0))
        queue = max(0.0, finite(post.get("queue_ahead"), 0.0))
        edge = finite(post.get("edge"), 0.0)
        prior_count, prior_volume = _flow(
            tape,
            token=token,
            limit_price=limit_price,
            receive_lo_ms=(post_ts - prior_lookback_seconds) * 1000,
            receive_hi_ms=post_ts * 1000,
        )
        future_count, future_volume = _flow(
            tape,
            token=token,
            limit_price=limit_price,
            event_lo_s=post_ts,
            event_hi_s=post_ts + forward_horizon_seconds,
        )
        prior_rate = prior_volume / max(1, prior_lookback_seconds)
        required = max(own + queue, 1e-12)
        projected_clearance = prior_rate * forward_horizon_seconds / required
        observed_clearance = future_volume / required
        active = prior_volume > 1e-12
        if active:
            flow_active += 1
        if projected_clearance > 0:
            projected_fill_positive += 1
        if future_volume > 0:
            future_compatible_positive += 1
        audited.append({
            "market_id": str(post.get("market_id") or ""),
            "side": str(post.get("side") or ""),
            "token_id": token,
            "limit_price": limit_price,
            "post_cost_edge": edge,
            "edge_above_authorized_floor": edge >= min_edge - 1e-12,
            "own_shares": own,
            "queue_ahead": queue,
            "queue_multiple": queue / own if own > 1e-12 else None,
            "prior_compatible_sell_trades": prior_count,
            "prior_compatible_sell_shares": prior_volume,
            "prior_compatible_sell_rate_per_second": prior_rate,
            "projected_queue_clearance_ratio": projected_clearance,
            "future_compatible_sell_trades": future_count,
            "future_compatible_sell_shares": future_volume,
            "observed_forward_clearance_ratio": observed_clearance,
        })

    pnl = realized_roundtrip_pnl(fill_rows)
    buy_shares = float(pnl["maker_buy_shares"])
    sold_shares = float(pnl["taker_exit_shares"])
    realized = float(pnl["realized_closed_pnl"])
    if sold_shares > 1e-12:
        decision = "POSITIVE_FILL_CONDITIONED_PNL" if realized > 0 else "FILLS_NEGATIVE_PNL"
    elif buy_shares > 1e-12:
        decision = "FILLS_NO_CLOSED_PNL_YET"
    elif flow_active > 0:
        decision = "ZERO_FILL_DESPITE_CAUSAL_FLOW"
    else:
        decision = "ZERO_FILL_DEAD_FLOW"

    actions: dict[str, int] = defaultdict(int)
    for row in orders:
        actions[str(row.get("action") or "UNKNOWN").upper()] += 1

    return {
        "decision": decision,
        "posted_orders": len(posts),
        "flow_active_posts": flow_active,
        "posts_with_positive_projected_clearance": projected_fill_positive,
        "posts_with_future_compatible_flow": future_compatible_positive,
        "prior_lookback_seconds": prior_lookback_seconds,
        "forward_horizon_seconds": forward_horizon_seconds,
        "authorized_post_cost_edge_floor": min_edge,
        "order_actions": dict(sorted(actions.items())),
        **pnl,
        "fill_conditioned_pnl_per_closed_share": realized / sold_shares if sold_shares > 1e-12 else None,
        "posts": audited,
    }


def main() -> int:
    parser = argparse.ArgumentParser(description="Audit forward maker fills, causal flow hazard and realized PnL")
    parser.add_argument("--order-log", type=Path, required=True)
    parser.add_argument("--trade-tape", type=Path, required=True)
    parser.add_argument("--fills", type=Path, required=True)
    parser.add_argument("--prior-lookback-seconds", type=int, default=120)
    parser.add_argument("--forward-horizon-seconds", type=int, default=60)
    parser.add_argument("--min-edge", type=float, default=0.00005)
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()
    result = audit(
        args.order_log,
        args.trade_tape,
        args.fills,
        prior_lookback_seconds=max(1, args.prior_lookback_seconds),
        forward_horizon_seconds=max(1, args.forward_horizon_seconds),
        min_edge=args.min_edge,
    )
    text = json.dumps(result, indent=2, sort_keys=True) + "\n"
    if args.output:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(text, encoding="utf-8")
    print(text, end="")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
