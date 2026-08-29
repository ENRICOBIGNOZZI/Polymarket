#!/usr/bin/env python3
from __future__ import annotations

import argparse
import csv
import datetime as dt
import json
import math
import os
import threading
import time
import urllib.parse
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from v7_market_common import finite, parse_array, request_json

FIELDS = [
    "bundle_id", "strategy", "event_id", "created_ts", "mode", "expected_edge",
    "max_notional", "market_id", "side", "weight", "limit_price",
    "execution_deadline_ts", "hold_deadline_ts",
]


def parse_ts(value: Any) -> int:
    if isinstance(value, (int, float)):
        raw = int(value)
        return raw // 1000 if raw > 10_000_000_000 else raw
    text = str(value or "").strip()
    if not text:
        return 0
    try:
        raw = int(float(text))
        return raw // 1000 if raw > 10_000_000_000 else raw
    except ValueError:
        pass
    try:
        parsed = dt.datetime.fromisoformat(text.replace("Z", "+00:00"))
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=dt.timezone.utc)
        return int(parsed.timestamp())
    except ValueError:
        return 0


@dataclass(frozen=True)
class Market:
    market_id: str
    condition_id: str
    event_id: str
    yes_token: str
    liquidity: float
    neg_risk: bool
    end_ts: int


@dataclass(frozen=True)
class Book:
    bid: float
    ask: float
    bid_size: float
    ask_size: float
    min_order: float


def parse_market(raw: dict[str, Any]) -> Market | None:
    ids = [str(item) for item in parse_array(raw.get("clobTokenIds"))]
    outcomes = [str(item).strip().lower() for item in parse_array(raw.get("outcomes"))]
    if len(ids) < 2:
        return None
    yes_index = 0
    for index, outcome in enumerate(outcomes[: len(ids)]):
        if outcome == "yes":
            yes_index = index
            break
    market_id = str(raw.get("id") or "").strip()
    condition_id = str(raw.get("conditionId") or "").strip()
    if not market_id or not condition_id:
        return None
    event_id = str(raw.get("eventId") or "").strip()
    events = raw.get("events")
    if not event_id and isinstance(events, list) and events and isinstance(events[0], dict):
        event_id = str(events[0].get("id") or "")
    return Market(
        market_id=market_id,
        condition_id=condition_id,
        event_id=event_id or condition_id,
        yes_token=ids[yes_index],
        liquidity=max(0.0, finite(raw.get("liquidityNum"), finite(raw.get("liquidity"), 0.0))),
        neg_risk=bool(raw.get("negRisk", False)),
        end_ts=parse_ts(raw.get("endDate") or raw.get("endDateIso")),
    )


def discover(gamma: str, limit: int, min_liquidity: float) -> list[Market]:
    output: list[Market] = []
    offset = 0
    while len(output) < limit and offset < 5000:
        query = urllib.parse.urlencode({
            "active": "true", "closed": "false", "limit": 100, "offset": offset,
            "order": "liquidityNum", "ascending": "false",
        })
        payload = request_json(f"{gamma.rstrip('/')}/markets?{query}")
        batch = payload if isinstance(payload, list) else payload.get("markets", []) if isinstance(payload, dict) else []
        if not batch:
            break
        for raw in batch:
            market = parse_market(raw) if isinstance(raw, dict) else None
            if market is not None and market.liquidity >= min_liquidity:
                output.append(market)
            if len(output) >= limit:
                break
        if len(batch) < 100:
            break
        offset += 100
    return output


def fetch_books(clob: str, tokens: list[str]) -> dict[str, Book]:
    output: dict[str, Book] = {}
    unique = list(dict.fromkeys(token for token in tokens if token))
    for start in range(0, len(unique), 80):
        rows = request_json(clob.rstrip("/") + "/books", [{"token_id": token} for token in unique[start:start + 80]])
        for raw in rows if isinstance(rows, list) else []:
            if not isinstance(raw, dict):
                continue
            token = str(raw.get("asset_id") or "")
            bids = sorted(
                ((finite(row.get("price")), finite(row.get("size"), 0.0)) for row in raw.get("bids", []) if isinstance(row, dict)),
                reverse=True,
            )
            asks = sorted((finite(row.get("price")), finite(row.get("size"), 0.0)) for row in raw.get("asks", []) if isinstance(row, dict))
            bids = [(price, size) for price, size in bids if math.isfinite(price) and 0 < price < 1 and size > 0]
            asks = [(price, size) for price, size in asks if math.isfinite(price) and 0 < price < 1 and size > 0]
            if token and bids and asks:
                output[token] = Book(bids[0][0], asks[0][0], bids[0][1], asks[0][1], max(1.0, finite(raw.get("min_order_size"), 1.0)))
    return output


def atomic_csv(path: Path, rows: list[dict[str, Any]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name + f".tmp.{os.getpid()}.{threading.get_ident()}")
    with temporary.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=FIELDS)
        writer.writeheader()
        writer.writerows(rows)
    os.replace(temporary, path)


def graph_rv_intents(
    gamma: str,
    clob: str,
    markets: list[Market],
    books: dict[str, Book],
    now: int,
    min_edge: float,
    max_trade: float,
    max_events: int,
) -> tuple[list[dict[str, Any]], dict[str, int]]:
    event_ids = list(dict.fromkeys(market.event_id for market in markets if market.neg_risk and market.event_id))
    rows: list[dict[str, Any]] = []
    stats = {"events_considered": 0, "events_complete": 0, "bundles": 0}
    serial = 0
    for event_id in event_ids[:max_events]:
        stats["events_considered"] += 1
        try:
            event = request_json(f"{gamma.rstrip('/')}/events/{event_id}")
        except Exception:
            continue
        if not isinstance(event, dict) or not event.get("negRisk") or event.get("negRiskAugmented"):
            continue
        raw_markets = event.get("markets")
        if not isinstance(raw_markets, list) or len(raw_markets) < 2:
            continue
        parsed = [parse_market(raw) for raw in raw_markets if isinstance(raw, dict)]
        if len(parsed) != len(raw_markets) or any(market is None for market in parsed):
            continue
        event_markets = [market for market in parsed if market is not None]
        missing = [market.yes_token for market in event_markets if market.yes_token not in books]
        if missing:
            try:
                books.update(fetch_books(clob, missing))
            except Exception:
                continue
        if any(market.yes_token not in books for market in event_markets):
            continue
        event_books = [books[market.yes_token] for market in event_markets]
        theoretical_cost = sum(book.bid for book in event_books)
        theoretical_edge = 1.0 - theoretical_cost
        if theoretical_edge <= min_edge:
            continue
        min_shares = min(book.bid_size for book in event_books)
        minimum_order = max(book.min_order for book in event_books)
        if min_shares + 1e-12 < minimum_order:
            continue
        max_notional = min(max_trade, min_shares * max(theoretical_cost, 1e-6))
        if max_notional <= 0.0:
            continue
        end_ts = max((market.end_ts for market in event_markets), default=0)
        execution_deadline = now + 180
        hold_deadline = max(now + 3600, end_ts + 3600 if end_ts else now + 7 * 86400)
        bundle_id = f"GRAPH_RV-{now}-{serial}"
        for market, book in zip(event_markets, event_books):
            rows.append({
                "bundle_id": bundle_id,
                "strategy": "GRAPH_RV",
                "event_id": event_id,
                "created_ts": now,
                "mode": "MAKER",
                "expected_edge": theoretical_edge,
                "max_notional": max_notional,
                "market_id": market.market_id,
                "side": "YES",
                "weight": 1.0,
                "limit_price": book.bid,
                "execution_deadline_ts": execution_deadline,
                "hold_deadline_ts": hold_deadline,
            })
        stats["events_complete"] += 1
        stats["bundles"] += 1
        serial += 1
    return rows, stats


def main() -> int:
    parser = argparse.ArgumentParser(description="V7 Graph/RV candidate scanner")
    parser.add_argument("--config", type=Path, default=Path("config/paper_v7.json"))
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--status", type=Path, required=True)
    parser.add_argument("--markets", type=int, default=1000)
    parser.add_argument("--min-liquidity", type=float, default=2.0)
    parser.add_argument("--min-edge", type=float, default=0.00005)
    parser.add_argument("--max-trade-usd", type=float, default=1e100)
    parser.add_argument("--max-events", type=int, default=80)
    args = parser.parse_args()
    cfg = json.loads(args.config.read_text(encoding="utf-8"))
    gamma, clob = str(cfg["gamma_url"]), str(cfg["clob_url"])
    now = int(time.time())
    failures: list[str] = []
    try:
        markets = discover(gamma, args.markets, args.min_liquidity)
        books = fetch_books(clob, [token for market in markets for token in [market.yes_token]])
    except Exception as exc:
        markets, books = [], {}
        failures.append(f"market_data:{type(exc).__name__}:{exc}")
    try:
        rows, stats = graph_rv_intents(gamma, clob, markets, books, now, args.min_edge, args.max_trade_usd, args.max_events)
    except Exception as exc:
        rows, stats = [], {"events_considered": 0, "events_complete": 0, "bundles": 0}
        failures.append(f"graph:{type(exc).__name__}:{exc}")
    rows.sort(key=lambda row: (float(row["expected_edge"]), row["bundle_id"]), reverse=True)
    atomic_csv(args.output, rows)
    status = {
        "schema": "polymarket_v7_graph_rv_scan_status_v1",
        "timestamp": now,
        "paper_only": True,
        "markets": len(markets),
        "books": len(books),
        "graph_rv": stats,
        "intent_rows": len(rows),
        "bundles": len({row["bundle_id"] for row in rows}),
        "best_theoretical_edge": max((float(row["expected_edge"]) for row in rows), default=0.0),
        "admission_is_theoretical_only": True,
        "downstream_joint_state_guard_required": True,
        "failures": failures,
    }
    args.status.parent.mkdir(parents=True, exist_ok=True)
    temporary = args.status.with_name(args.status.name + f".tmp.{os.getpid()}.{threading.get_ident()}")
    temporary.write_text(json.dumps(status, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    os.replace(temporary, args.status)
    print(json.dumps(status, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
