#!/usr/bin/env python3
"""Inventory the complete active Polymarket universe using keyset pagination.

This is deliberately public-data only. It creates a cheap Tier-0 inventory and
liquidity-based Tier-1/Tier-2 views that downstream engines can refine with
live spread/depth and model-specific requirements.
"""
from __future__ import annotations

import argparse
import csv
import json
import os
import tempfile
import time
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Any

FIELDS = [
    "market_id",
    "condition_id",
    "event_id",
    "slug",
    "question",
    "liquidity",
    "volume24h",
    "active",
    "closed",
    "enable_order_book",
    "accepting_orders",
    "neg_risk",
    "yes_token",
    "no_token",
    "tier",
]


def fnum(value: Any, default: float = 0.0) -> float:
    try:
        out = float(value)
    except (TypeError, ValueError):
        return default
    return out if out == out and abs(out) != float("inf") else default


def truth(value: Any, default: bool = False) -> bool:
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return value != 0
    if isinstance(value, str):
        raw = value.strip().lower()
        if raw in {"true", "1", "yes"}:
            return True
        if raw in {"false", "0", "no"}:
            return False
    return default


def string_list(value: Any) -> list[str]:
    if isinstance(value, str):
        try:
            value = json.loads(value)
        except json.JSONDecodeError:
            return [value] if value else []
    if isinstance(value, list):
        return [str(item) for item in value]
    return []


def event_id(market: dict[str, Any]) -> str:
    direct = str(market.get("eventId") or "")
    if direct:
        return direct
    events = market.get("events")
    if isinstance(events, list) and events and isinstance(events[0], dict):
        value = events[0].get("id")
        if value is not None:
            return str(value)
    return str(market.get("conditionId") or market.get("id") or "")


def normalized_market(market: dict[str, Any], tier1: float, tier2: float) -> dict[str, Any] | None:
    active = truth(market.get("active"), True)
    closed = truth(market.get("closed"), False)
    enabled = truth(market.get("enableOrderBook"), True)
    accepting = truth(market.get("acceptingOrders"), True)
    if not active or closed or not enabled or not accepting:
        return None
    tokens = string_list(market.get("clobTokenIds"))
    outcomes = [item.lower() for item in string_list(market.get("outcomes"))]
    if len(tokens) < 2:
        return None
    yes_index, no_index = 0, 1
    for index, outcome in enumerate(outcomes[: len(tokens)]):
        if outcome == "yes":
            yes_index = index
        elif outcome == "no":
            no_index = index
    liquidity = fnum(market.get("liquidityNum", market.get("liquidity", 0.0)))
    volume24h = fnum(market.get("volume24hr", 0.0))
    tier = 0
    if liquidity >= tier1:
        tier = 1
    if liquidity >= tier2:
        tier = 2
    return {
        "market_id": str(market.get("id") or ""),
        "condition_id": str(market.get("conditionId") or ""),
        "event_id": event_id(market),
        "slug": str(market.get("slug") or ""),
        "question": str(market.get("question") or ""),
        "liquidity": liquidity,
        "volume24h": volume24h,
        "active": 1,
        "closed": 0,
        "enable_order_book": 1,
        "accepting_orders": 1,
        "neg_risk": int(truth(market.get("negRisk"), False)),
        "yes_token": tokens[yes_index],
        "no_token": tokens[no_index],
        "tier": tier,
    }


def fetch_json(url: str, timeout: float) -> dict[str, Any]:
    request = urllib.request.Request(url, headers={"User-Agent": "polymarket-all-market-universe/1.0"})
    with urllib.request.urlopen(request, timeout=timeout) as response:
        payload = json.loads(response.read().decode("utf-8"))
    if not isinstance(payload, dict):
        raise RuntimeError("unexpected keyset response")
    return payload


def discover(base_url: str, page_size: int, tier1: float, tier2: float, limit: int, timeout: float) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    seen_markets: set[str] = set()
    seen_cursors: set[str] = set()
    cursor = ""
    while limit <= 0 or len(rows) < limit:
        # Keep the cursor request deliberately minimal. Tradability is filtered
        # below from the returned market object so API filter drift cannot shrink
        # the research universe silently.
        query = {
            "closed": "false",
            "limit": str(max(1, min(100, page_size))),
        }
        if cursor:
            query["after_cursor"] = cursor
        url = base_url.rstrip("/") + "/markets/keyset?" + urllib.parse.urlencode(query)
        payload = fetch_json(url, timeout)
        markets = payload.get("markets")
        if not isinstance(markets, list):
            raise RuntimeError("keyset response has no markets array")
        for market in markets:
            if not isinstance(market, dict):
                continue
            row = normalized_market(market, tier1, tier2)
            if not row or not row["market_id"] or row["market_id"] in seen_markets:
                continue
            seen_markets.add(str(row["market_id"]))
            rows.append(row)
            if limit > 0 and len(rows) >= limit:
                break
        next_cursor = str(payload.get("next_cursor") or "")
        if not next_cursor or next_cursor == cursor or next_cursor in seen_cursors:
            break
        seen_cursors.add(next_cursor)
        cursor = next_cursor
        if not markets:
            break
    rows.sort(key=lambda row: (-int(row["tier"]), -float(row["liquidity"]), -float(row["volume24h"]), str(row["market_id"])))
    return rows


def atomic_csv(path: Path, rows: list[dict[str, Any]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temp_name = tempfile.mkstemp(prefix=path.name + ".", suffix=".tmp", dir=path.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="") as handle:
            writer = csv.DictWriter(handle, fieldnames=FIELDS)
            writer.writeheader()
            writer.writerows(rows)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temp_name, path)
    finally:
        try:
            os.unlink(temp_name)
        except FileNotFoundError:
            pass


def atomic_json(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temp_name = tempfile.mkstemp(prefix=path.name + ".", suffix=".tmp", dir=path.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(payload, handle, indent=2, sort_keys=True)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temp_name, path)
    finally:
        try:
            os.unlink(temp_name)
        except FileNotFoundError:
            pass


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--gamma-url", default="https://gamma-api.polymarket.com")
    parser.add_argument("--output", type=Path, default=Path("runs/paper_v4_live/all_market/universe.csv"))
    parser.add_argument("--status", type=Path, default=Path("runs/paper_v4_live/all_market/universe_status.json"))
    parser.add_argument("--page-size", type=int, default=100)
    parser.add_argument("--tier1-min-liquidity", type=float, default=20.0)
    parser.add_argument("--tier2-min-liquidity", type=float, default=100.0)
    parser.add_argument("--limit", type=int, default=0, help="0 means all active tradable markets")
    parser.add_argument("--timeout", type=float, default=20.0)
    args = parser.parse_args()

    generated = int(time.time())
    rows = discover(
        args.gamma_url,
        args.page_size,
        args.tier1_min_liquidity,
        args.tier2_min_liquidity,
        args.limit,
        args.timeout,
    )
    atomic_csv(args.output, rows)
    counts = {str(tier): sum(int(row["tier"]) == tier for row in rows) for tier in range(3)}
    status = {
        "schema": "polymarket_all_market_universe_v1",
        "generated_ts": generated,
        "markets": len(rows),
        "tier0_only": counts["0"],
        "tier1": counts["1"] + counts["2"],
        "tier2": counts["2"],
        "limit": args.limit,
        "page_size": max(1, min(100, args.page_size)),
    }
    atomic_json(args.status, status)
    print(json.dumps(status, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
