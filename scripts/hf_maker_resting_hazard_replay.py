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
    except (TypeError, ValueError, OverflowError):
        return default
    return x if math.isfinite(x) else default


def read_rows(path: Path) -> list[dict[str, str]]:
    if not path.exists() or path.stat().st_size == 0:
        return []
    with path.open(newline="", encoding="utf-8") as handle:
        return [dict(row) for row in csv.DictReader(handle)]


def _compatible(row: dict[str, str], *, token: str, limit_price: float) -> bool:
    if str(row.get("asset_id") or row.get("token_id") or "") != token:
        return False
    if str(row.get("side") or "").upper() != "SELL":
        return False
    price = finite(row.get("price"))
    size = finite(row.get("size"), 0.0)
    return math.isfinite(price) and price <= limit_price + 1e-12 and size > 0.0


def _cancel_timestamp(order_rows: list[dict[str, str]], post: dict[str, str], ttl_seconds: int) -> int:
    post_ts = int(finite(post.get("timestamp"), 0.0))
    market = str(post.get("market_id") or "")
    token = str(post.get("token_id") or "")
    candidates = []
    for row in order_rows:
        action = str(row.get("action") or "").upper()
        ts = int(finite(row.get("timestamp"), 0.0))
        if ts <= post_ts or not action.startswith("CANCEL"):
            continue
        if str(row.get("market_id") or "") == market and str(row.get("token_id") or "") == token:
            candidates.append(ts)
    return min(candidates) if candidates else post_ts + max(1, int(ttl_seconds))


def replay_post(
    post: dict[str, str],
    order_rows: list[dict[str, str]],
    tape_rows: list[dict[str, str]],
    *,
    ttl_seconds: int = 60,
    grace_seconds: int = 20,
    stale_receive_seconds: int = 20,
    revalidate_seconds: int = 5,
    prior_lookback_seconds: int = 120,
) -> dict[str, Any]:
    post_ts = int(finite(post.get("timestamp"), 0.0))
    token = str(post.get("token_id") or "")
    market = str(post.get("market_id") or "")
    limit_price = finite(post.get("limit_price"), 0.0)
    own = max(0.0, finite(post.get("remaining_shares"), 0.0))
    queue = max(0.0, finite(post.get("queue_ahead"), 0.0))
    required = queue + own
    notional = own * limit_price
    static_end = min(_cancel_timestamp(order_rows, post, ttl_seconds), post_ts + max(1, int(ttl_seconds)))

    compatible = [r for r in tape_rows if _compatible(r, token=token, limit_price=limit_price)]
    prior = []
    future = []
    for row in compatible:
        event_ts = int(finite(row.get("timestamp"), 0.0))
        received_ms = int(finite(row.get("received_ms"), event_ts * 1000.0))
        if post_ts - prior_lookback_seconds <= event_ts <= post_ts and received_ms <= post_ts * 1000:
            prior.append((received_ms, event_ts, max(0.0, finite(row.get("size"), 0.0))))
        if event_ts > post_ts and received_ms > post_ts * 1000 and event_ts <= post_ts + ttl_seconds:
            future.append((received_ms, event_ts, max(0.0, finite(row.get("size"), 0.0))))
    prior.sort()
    future.sort()

    last_receive_ms = max((x[0] for x in prior), default=post_ts * 1000)
    cumulative = 0.0
    fill_receive_ms: int | None = None
    for received_ms, _event_ts, size in future:
        if received_ms > static_end * 1000:
            continue
        cumulative += size
        if fill_receive_ms is None and cumulative + 1e-12 >= required:
            fill_receive_ms = received_ms
            break

    dynamic_cancel_ts: int | None = None
    observed_before_cancel = 0.0
    latest_receive_ms = last_receive_ms
    future_index = 0
    checkpoint = post_ts + max(1, int(grace_seconds))
    dynamic_fill_receive_ms: int | None = None
    while checkpoint <= static_end:
        while future_index < len(future) and future[future_index][0] <= checkpoint * 1000:
            received_ms, event_ts, size = future[future_index]
            if event_ts <= post_ts + ttl_seconds:
                observed_before_cancel += size
                latest_receive_ms = max(latest_receive_ms, received_ms)
                if observed_before_cancel + 1e-12 >= required:
                    dynamic_fill_receive_ms = received_ms
                    break
            future_index += 1
        if dynamic_fill_receive_ms is not None:
            break
        receive_age = checkpoint - latest_receive_ms / 1000.0
        if receive_age >= max(1, int(stale_receive_seconds)):
            dynamic_cancel_ts = checkpoint
            break
        checkpoint += max(1, int(revalidate_seconds))

    if dynamic_fill_receive_ms is not None:
        dynamic_end = min(static_end, math.ceil(dynamic_fill_receive_ms / 1000.0))
        dynamic_outcome = "FILL_BEFORE_STALE_CANCEL"
    elif dynamic_cancel_ts is not None:
        dynamic_end = dynamic_cancel_ts
        dynamic_outcome = "CANCEL_STALE_RESTING_HAZARD"
    else:
        dynamic_end = static_end
        dynamic_outcome = "STATIC_END"

    static_fill = fill_receive_ms is not None
    dynamic_fill = dynamic_fill_receive_ms is not None
    static_capital_seconds = notional * max(0, static_end - post_ts)
    dynamic_capital_seconds = notional * max(0, dynamic_end - post_ts)
    saved = max(0.0, static_capital_seconds - dynamic_capital_seconds)

    return {
        "market_id": market,
        "token_id": token,
        "side": str(post.get("side") or ""),
        "post_ts": post_ts,
        "limit_price": limit_price,
        "own_shares": own,
        "queue_ahead": queue,
        "required_queue_plus_own": required,
        "recorded_fill_probability_proxy": finite(post.get("fill_probability"), 0.0),
        "recorded_flow_rate": finite(post.get("flow_rate"), 0.0),
        "static_end_ts": static_end,
        "dynamic_end_ts": dynamic_end,
        "dynamic_outcome": dynamic_outcome,
        "static_fill_before_end": static_fill,
        "dynamic_fill_before_cancel": dynamic_fill,
        "missed_fill_due_dynamic_cancel": static_fill and not dynamic_fill,
        "future_compatible_shares_before_static_end": cumulative,
        "queue_cleared_before_static_end": cumulative + 1e-12 >= queue,
        "own_size_cleared_before_static_end": cumulative + 1e-12 >= required,
        "static_capital_seconds": static_capital_seconds,
        "dynamic_capital_seconds": dynamic_capital_seconds,
        "capital_seconds_saved": saved,
    }


def replay(
    order_log: Path,
    trade_tape: Path,
    *,
    ttl_seconds: int = 60,
    grace_seconds: int = 20,
    stale_receive_seconds: int = 20,
    revalidate_seconds: int = 5,
    prior_lookback_seconds: int = 120,
) -> dict[str, Any]:
    orders = read_rows(order_log)
    tape = read_rows(trade_tape)
    posts = [r for r in orders if str(r.get("action") or "").upper() == "POST"]
    rows = [
        replay_post(
            post,
            orders,
            tape,
            ttl_seconds=ttl_seconds,
            grace_seconds=grace_seconds,
            stale_receive_seconds=stale_receive_seconds,
            revalidate_seconds=revalidate_seconds,
            prior_lookback_seconds=prior_lookback_seconds,
        )
        for post in posts
    ]
    static_capital = sum(float(r["static_capital_seconds"]) for r in rows)
    dynamic_capital = sum(float(r["dynamic_capital_seconds"]) for r in rows)
    saved = sum(float(r["capital_seconds_saved"]) for r in rows)
    return {
        "paper_only": True,
        "authenticated_execution": False,
        "policy": {
            "ttl_seconds": ttl_seconds,
            "grace_seconds": grace_seconds,
            "stale_receive_seconds": stale_receive_seconds,
            "revalidate_seconds": revalidate_seconds,
            "prior_lookback_seconds": prior_lookback_seconds,
            "causal_clock": "received_ms for information availability; event timestamp must be after post for fill eligibility",
        },
        "posted_orders": len(rows),
        "static_fills": sum(bool(r["static_fill_before_end"]) for r in rows),
        "dynamic_fills": sum(bool(r["dynamic_fill_before_cancel"]) for r in rows),
        "missed_fills_due_dynamic_cancel": sum(bool(r["missed_fill_due_dynamic_cancel"]) for r in rows),
        "stale_hazard_cancels": sum(r["dynamic_outcome"] == "CANCEL_STALE_RESTING_HAZARD" for r in rows),
        "static_capital_seconds": static_capital,
        "dynamic_capital_seconds": dynamic_capital,
        "capital_seconds_saved": saved,
        "capital_time_reduction_fraction": saved / static_capital if static_capital > 1e-12 else 0.0,
        "rows": rows,
    }


def main() -> int:
    parser = argparse.ArgumentParser(description="Causal replay of refreshed resting maker hazard")
    parser.add_argument("--order-log", type=Path, required=True)
    parser.add_argument("--trade-tape", type=Path, required=True)
    parser.add_argument("--ttl-seconds", type=int, default=60)
    parser.add_argument("--grace-seconds", type=int, default=20)
    parser.add_argument("--stale-receive-seconds", type=int, default=20)
    parser.add_argument("--revalidate-seconds", type=int, default=5)
    parser.add_argument("--prior-lookback-seconds", type=int, default=120)
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()
    result = replay(
        args.order_log,
        args.trade_tape,
        ttl_seconds=max(1, args.ttl_seconds),
        grace_seconds=max(1, args.grace_seconds),
        stale_receive_seconds=max(1, args.stale_receive_seconds),
        revalidate_seconds=max(1, args.revalidate_seconds),
        prior_lookback_seconds=max(1, args.prior_lookback_seconds),
    )
    text = json.dumps(result, indent=2, sort_keys=True) + "\n"
    if args.output:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(text, encoding="utf-8")
    print(text, end="")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
