#!/usr/bin/env python3
from __future__ import annotations

import argparse
import csv
import datetime as dt
import json
import math
import time
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Any


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


def parse_ts(value: Any) -> int:
    if isinstance(value, (int, float)):
        return int(value)
    raw = str(value or "").strip()
    if not raw:
        return 0
    try:
        return int(float(raw))
    except ValueError:
        pass
    try:
        return int(dt.datetime.fromisoformat(raw.replace("Z", "+00:00")).timestamp())
    except ValueError:
        return 0


def fetch_trades(
    data_url: str,
    condition_id: str,
    *,
    start_ts: int,
    end_ts: int,
    timeout: float,
    limit: int,
) -> list[dict[str, Any]]:
    query = urllib.parse.urlencode(
        {
            "market": condition_id,
            "limit": max(1, min(int(limit), 10000)),
            "takerOnly": "true",
            "start": start_ts,
            "end": end_ts,
        }
    )
    req = urllib.request.Request(
        f"{data_url.rstrip('/')}/trades?{query}",
        headers={"User-Agent": "polymarket-v6-maker-flow-audit/1.0"},
    )
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        root = json.loads(resp.read().decode("utf-8"))
    if isinstance(root, list):
        rows = root
    elif isinstance(root, dict) and isinstance(root.get("data"), list):
        rows = root["data"]
    else:
        raise ValueError("unexpected Data API trades response")
    return [row for row in rows if isinstance(row, dict)]


def evaluate_order(
    order: dict[str, str],
    trades: list[dict[str, Any]],
    *,
    lookback_seconds: int,
    ttl_seconds: int,
) -> dict[str, Any]:
    created = _i(order.get("created_ts"))
    start = created - max(1, int(lookback_seconds))
    token_id = str(order.get("token_id") or "")
    limit_price = _f(order.get("limit_price"), math.nan)
    queue_ahead = max(0.0, _f(order.get("queue_ahead"), 0.0))
    shares = max(0.0, _f(order.get("remaining_shares"), 0.0))

    matching: list[dict[str, Any]] = []
    all_token_sells = 0
    all_token_sell_size = 0.0
    for row in trades:
        ts = parse_ts(row.get("timestamp"))
        # The audit is strictly pre-decision: observations after order creation
        # are never used to decide whether the order would have been admitted.
        if ts < start or ts > created:
            continue
        asset = str(row.get("asset") or row.get("token_id") or "")
        side = str(row.get("side") or "").upper()
        if asset != token_id or side != "SELL":
            continue
        size = max(0.0, _f(row.get("size"), 0.0))
        price = _f(row.get("price"), math.nan)
        if size <= 0.0 or not math.isfinite(price):
            continue
        all_token_sells += 1
        all_token_sell_size += size
        # Passive bid fills require a taker SELL at our bid or through it.
        if math.isfinite(limit_price) and price <= limit_price + 1e-12:
            matching.append({"ts": ts, "price": price, "size": size})

    eligible_size = sum(float(row["size"]) for row in matching)
    eligible_prints = len(matching)
    flow_per_second = eligible_size / max(1, int(lookback_seconds))
    depletion_seconds = queue_ahead / flow_per_second if flow_per_second > 1e-12 else None
    ttl = max(1, int(ttl_seconds))
    fillable_within_ttl = bool(
        eligible_prints > 0 and depletion_seconds is not None and depletion_seconds <= ttl
    )
    ratio = queue_ahead / max(shares, 1e-12)
    return {
        "market_id": order.get("market_id", ""),
        "condition_id": order.get("condition_id", ""),
        "token_id": token_id,
        "side": order.get("side", ""),
        "created_ts": created,
        "lookback_start_ts": start,
        "lookback_seconds": int(lookback_seconds),
        "ttl_seconds": ttl,
        "limit_price": limit_price,
        "remaining_shares": shares,
        "queue_ahead": queue_ahead,
        "queue_ratio": ratio,
        "token_sell_prints": all_token_sells,
        "token_sell_size": all_token_sell_size,
        "eligible_contra_prints": eligible_prints,
        "eligible_contra_size": eligible_size,
        "eligible_contra_flow_per_second": flow_per_second,
        "estimated_queue_depletion_seconds": depletion_seconds,
        "estimated_depletion_to_ttl": depletion_seconds / ttl if depletion_seconds is not None else None,
        "fillable_within_ttl": fillable_within_ttl,
    }


def run(args: argparse.Namespace) -> dict[str, Any]:
    orders_path = Path(args.orders)
    with orders_path.open(newline="", encoding="utf-8") as fh:
        orders = list(csv.DictReader(fh))
    if args.max_orders > 0:
        orders = orders[: args.max_orders]

    rows: list[dict[str, Any]] = []
    failures: list[dict[str, str]] = []
    for index, order in enumerate(orders):
        condition_id = str(order.get("condition_id") or "")
        created = _i(order.get("created_ts"))
        if not condition_id or created <= 0:
            failures.append({"market_id": order.get("market_id", ""), "error": "missing_condition_or_created_ts"})
            continue
        try:
            trades = fetch_trades(
                args.data_url,
                condition_id,
                start_ts=created - args.lookback_seconds,
                end_ts=created,
                timeout=args.timeout,
                limit=args.limit,
            )
            rows.append(
                evaluate_order(
                    order,
                    trades,
                    lookback_seconds=args.lookback_seconds,
                    ttl_seconds=args.ttl_seconds,
                )
            )
        except Exception as exc:
            failures.append(
                {
                    "market_id": order.get("market_id", ""),
                    "error": f"{type(exc).__name__}:{exc}",
                }
            )
        if args.sleep_seconds > 0 and index + 1 < len(orders):
            time.sleep(args.sleep_seconds)

    finite_depletion = [
        float(row["estimated_queue_depletion_seconds"])
        for row in rows
        if row.get("estimated_queue_depletion_seconds") is not None
    ]
    sorted_depletion = sorted(finite_depletion)
    median_depletion = (
        sorted_depletion[len(sorted_depletion) // 2] if sorted_depletion else None
    )
    summary = {
        "schema": "polymarket_v6_maker_prepost_flow_audit_v1",
        "paper_only": True,
        "authenticated_execution": False,
        "causal_window": "strictly at or before order.created_ts",
        "orders_seen": len(orders),
        "orders_evaluated": len(rows),
        "request_failures": failures,
        "lookback_seconds": args.lookback_seconds,
        "ttl_seconds": args.ttl_seconds,
        "orders_with_any_token_sell": sum(int(row["token_sell_prints"]) > 0 for row in rows),
        "orders_with_eligible_contra_sell": sum(int(row["eligible_contra_prints"]) > 0 for row in rows),
        "orders_fillable_within_ttl_by_flow_proxy": sum(bool(row["fillable_within_ttl"]) for row in rows),
        "estimated_queue_depletion_seconds_min": min(finite_depletion) if finite_depletion else None,
        "estimated_queue_depletion_seconds_median": median_depletion,
        "estimated_queue_depletion_seconds_max": max(finite_depletion) if finite_depletion else None,
        "orders": rows,
    }
    Path(args.output).write_text(json.dumps(summary, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    print(json.dumps(summary, indent=2, sort_keys=True))
    return summary


def main() -> int:
    parser = argparse.ArgumentParser(description="Causal pre-post maker contra-flow audit")
    parser.add_argument("--orders", required=True)
    parser.add_argument("--output", required=True)
    parser.add_argument("--data-url", default="https://data-api.polymarket.com")
    parser.add_argument("--lookback-seconds", type=int, default=900)
    parser.add_argument("--ttl-seconds", type=int, default=30)
    parser.add_argument("--limit", type=int, default=1000)
    parser.add_argument("--timeout", type=float, default=8.0)
    parser.add_argument("--sleep-seconds", type=float, default=0.03)
    parser.add_argument("--max-orders", type=int, default=80)
    args = parser.parse_args()
    run(args)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
