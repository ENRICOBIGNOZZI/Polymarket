#!/usr/bin/env python3
from __future__ import annotations

import argparse
import csv
import json
import math
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


def audit(order_log: Path, trade_tape: Path, max_queue_multiple: float = 6.0) -> dict[str, Any]:
    orders = read_rows(order_log)
    tape = read_rows(trade_tape)
    posts = [row for row in orders if str(row.get("action") or "").upper() == "POST"]
    queue_skips = sum(str(row.get("action") or "").upper() == "SKIP_QUEUE" for row in orders)

    rows: list[dict[str, Any]] = []
    for post in posts:
        post_ts = int(finite(post.get("timestamp"), 0.0))
        token = str(post.get("token_id") or "")
        limit_price = finite(post.get("limit_price"), 0.0)
        shares = max(0.0, finite(post.get("remaining_shares"), 0.0))
        queue = max(0.0, finite(post.get("queue_ahead"), 0.0))
        queue_multiple = queue / shares if shares > 1e-12 else math.inf

        compatible_volume = 0.0
        compatible_trades = 0
        for trade in tape:
            if int(finite(trade.get("timestamp"), 0.0)) <= post_ts:
                continue
            asset = str(trade.get("asset_id") or trade.get("token_id") or "")
            if asset != token:
                continue
            if str(trade.get("side") or "").upper() != "SELL":
                continue
            price = finite(trade.get("price"))
            size = max(0.0, finite(trade.get("size"), 0.0))
            if math.isfinite(price) and price <= limit_price + 1e-12 and size > 0.0:
                compatible_trades += 1
                compatible_volume += size

        clearance = compatible_volume / queue if queue > 1e-12 else (math.inf if compatible_volume > 0 else 0.0)
        rows.append(
            {
                "market_id": str(post.get("market_id") or ""),
                "side": str(post.get("side") or ""),
                "limit_price": limit_price,
                "shares": shares,
                "queue_ahead": queue,
                "queue_multiple": queue_multiple,
                "passes_queue_multiple_gate": queue_multiple <= max_queue_multiple + 1e-12,
                "compatible_post_entry_trades": compatible_trades,
                "compatible_post_entry_volume": compatible_volume,
                "observed_queue_clearance_fraction": clearance,
            }
        )

    multiples = [float(row["queue_multiple"]) for row in rows if math.isfinite(float(row["queue_multiple"]))]
    zero_flow = sum(row["compatible_post_entry_volume"] <= 1e-12 for row in rows)
    mechanical_quarter_touch = sum(abs(row["queue_multiple"] - 4.0) <= 0.05 for row in rows)
    result = {
        "posted_orders": len(rows),
        "queue_skipped": queue_skips,
        "max_queue_multiple": max_queue_multiple,
        "queue_multiple_min": min(multiples) if multiples else None,
        "queue_multiple_median": sorted(multiples)[len(multiples) // 2] if multiples else None,
        "queue_multiple_max": max(multiples) if multiples else None,
        "posted_with_zero_compatible_post_entry_flow": zero_flow,
        "posted_near_mechanical_four_x_queue_multiple": mechanical_quarter_touch,
        "structural_queue_gate_problem": bool(rows) and mechanical_quarter_touch > 0 and zero_flow > 0,
        "decision": "FLOW_HAZARD_REQUIRED" if rows and mechanical_quarter_touch > 0 and zero_flow > 0 else "NO_STRUCTURAL_QUEUE_GATE_FINDING",
        "orders": rows,
    }
    return result


def main() -> int:
    parser = argparse.ArgumentParser(description="Audit maker FIFO admission against causal post-entry contra-flow")
    parser.add_argument("--order-log", type=Path, required=True)
    parser.add_argument("--trade-tape", type=Path, required=True)
    parser.add_argument("--max-queue-multiple", type=float, default=6.0)
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()
    result = audit(args.order_log, args.trade_tape, args.max_queue_multiple)
    text = json.dumps(result, indent=2, sort_keys=True) + "\n"
    if args.output:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(text, encoding="utf-8")
    print(text, end="")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
