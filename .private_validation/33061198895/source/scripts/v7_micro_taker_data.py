#!/usr/bin/env python3
from __future__ import annotations

import csv
import json
import math
import os
import threading
import time
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Any

from polymarket_fees import FeeDetails, fee_per_share, parse_fee_details, resolve_fee_details
from v7_micro_target import label_matured_samples


def finite(value: Any, default: float = math.nan) -> float:
    try:
        x = float(value)
    except (TypeError, ValueError, OverflowError):
        return default
    return x if math.isfinite(x) else default


def epoch_seconds(value: Any) -> int:
    """Normalize public API timestamps to whole Unix seconds, failing closed on invalid values."""
    x = finite(value)
    if not math.isfinite(x) or x <= 0.0:
        return 0
    if x >= 1e17:
        x /= 1e9
    elif x >= 1e14:
        x /= 1e6
    elif x >= 1e11:
        x /= 1e3
    return int(x) if 0.0 < x < 1e11 else 0


def request_json(url: str, payload: Any | None = None, timeout: int = 20) -> Any:
    data = None if payload is None else json.dumps(payload).encode("utf-8")
    req = urllib.request.Request(
        url,
        data=data,
        headers={"User-Agent": "polymarket-v7-paper/2", "Content-Type": "application/json"},
    )
    with urllib.request.urlopen(req, timeout=timeout) as response:
        return json.loads(response.read().decode("utf-8"))


def parse_array(value: Any) -> list[Any]:
    if isinstance(value, list):
        return value
    if isinstance(value, str):
        try:
            out = json.loads(value)
            return out if isinstance(out, list) else []
        except json.JSONDecodeError:
            return []
    return []


def atomic_json(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + f".tmp.{os.getpid()}.{threading.get_ident()}")
    tmp.write_text(json.dumps(value, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    os.replace(tmp, path)


def append_csv(path: Path, fields: list[str], row: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    exists = path.exists() and path.stat().st_size > 0
    with path.open("a", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=fields)
        if not exists:
            writer.writeheader()
        writer.writerow({key: row.get(key, "") for key in fields})


class Market:
    def __init__(self, raw: dict[str, Any]):
        ids = [str(x) for x in parse_array(raw.get("clobTokenIds"))]
        outcomes = [str(x).lower() for x in parse_array(raw.get("outcomes"))]
        if len(ids) < 2:
            raise ValueError("missing tokens")
        yi, ni = 0, 1
        for i, outcome in enumerate(outcomes[: len(ids)]):
            if outcome == "yes":
                yi = i
            elif outcome == "no":
                ni = i
        self.id = str(raw.get("id") or "")
        self.condition = str(raw.get("conditionId") or "")
        self.event = str(raw.get("eventId") or self.condition or self.id)
        events = raw.get("events")
        if isinstance(events, list) and events and isinstance(events[0], dict):
            self.event = str(events[0].get("id") or self.event)
        self.slug = str(raw.get("slug") or self.id)
        self.yes = ids[yi]
        self.no = ids[ni]
        self.liq = max(0.0, finite(raw.get("liquidityNum"), 0.0))
        self.raw = raw
        self.fee: FeeDetails | None = parse_fee_details(raw)


class Book:
    def __init__(self, raw: dict[str, Any], received_ts: int | None = None):
        self.token = str(raw.get("asset_id") or "")
        self.exchange_ts = epoch_seconds(raw.get("timestamp"))
        local_received = time.time() if received_ts is None else finite(received_ts)
        self.received_ts = int(local_received) if math.isfinite(local_received) and local_received > 0.0 else 0
        self.tick = max(1e-6, finite(raw.get("tick_size"), 0.01))
        self.min_order = max(1.0, finite(raw.get("min_order_size"), 1.0))
        self.bids: list[tuple[float, float]] = []
        self.asks: list[tuple[float, float]] = []
        for row in raw.get("bids", []):
            if isinstance(row, dict):
                price, size = finite(row.get("price")), finite(row.get("size"), 0.0)
                if math.isfinite(price) and 0 < price < 1 and size > 0:
                    self.bids.append((price, size))
        for row in raw.get("asks", []):
            if isinstance(row, dict):
                price, size = finite(row.get("price")), finite(row.get("size"), 0.0)
                if math.isfinite(price) and 0 < price < 1 and size > 0:
                    self.asks.append((price, size))
        self.bids.sort(reverse=True)
        self.asks.sort()

    def freshness_ts(self) -> int:
        if self.exchange_ts <= 0 or self.received_ts <= 0:
            return 0
        return min(self.exchange_ts, self.received_ts)

    def bid(self) -> float:
        return self.bids[0][0] if self.bids else math.nan

    def ask(self) -> float:
        return self.asks[0][0] if self.asks else math.nan

    def mid(self) -> float:
        bid, ask = self.bid(), self.ask()
        return 0.5 * (ask + bid) if math.isfinite(ask) and math.isfinite(bid) else math.nan

    def spread(self) -> float:
        bid, ask = self.bid(), self.ask()
        return ask - bid if math.isfinite(ask) and math.isfinite(bid) else math.nan

    def depth(self, bid_side: bool, n: int = 5) -> float:
        levels = self.bids if bid_side else self.asks
        if not levels:
            return 0.0
        best = levels[0][0]
        scale = max(1e-4, 3 * self.tick)
        return sum(size * math.exp(-abs(price - best) / scale) for price, size in levels[:n])

    def micro(self) -> float:
        bid, ask = self.bid(), self.ask()
        db, da = self.depth(True), self.depth(False)
        if not math.isfinite(bid) or not math.isfinite(ask):
            return math.nan
        return (ask * db + bid * da) / (db + da) if db + da > 1e-12 else 0.5 * (ask + bid)


def discover(gamma: str, limit: int, min_liq: float) -> list[Market]:
    out: list[Market] = []
    offset = 0
    while len(out) < limit and offset < 4000:
        query = urllib.parse.urlencode({
            "active": "true",
            "closed": "false",
            "limit": 100,
            "offset": offset,
            "order": "liquidityNum",
            "ascending": "false",
        })
        raw = request_json(gamma.rstrip("/") + "/markets?" + query)
        batch = raw if isinstance(raw, list) else raw.get("markets", []) if isinstance(raw, dict) else []
        if not batch:
            break
        for row in batch:
            if not isinstance(row, dict):
                continue
            try:
                market = Market(row)
            except ValueError:
                continue
            if market.id and market.condition and market.liq >= min_liq:
                out.append(market)
            if len(out) >= limit:
                break
        if len(batch) < 100:
            break
        offset += 100
    return out


def fetch_books(clob: str, markets: list[Market]) -> dict[str, Book]:
    tokens = [token for market in markets for token in (market.yes, market.no)]
    out: dict[str, Book] = {}
    for i in range(0, len(tokens), 80):
        raw = request_json(clob.rstrip("/") + "/books", [{"token_id": token} for token in tokens[i : i + 80]])
        received_ts = int(time.time())
        for row in raw if isinstance(raw, list) else []:
            if not isinstance(row, dict):
                continue
            book = Book(row, received_ts=received_ts)
            if book.token and book.bids and book.asks:
                out[book.token] = book
    return out


def features(yes: Book, no: Book) -> tuple[list[float], float, float] | None:
    mid = yes.mid()
    spread = max(yes.spread(), no.spread())
    if not math.isfinite(mid) or not math.isfinite(spread) or spread <= 0:
        return None
    yes_micro, no_micro = yes.micro(), no.micro()
    yes_bid_depth, yes_ask_depth = yes.depth(True), yes.depth(False)
    no_bid_depth, no_ask_depth = no.depth(True), no.depth(False)
    if not math.isfinite(yes_micro) or not math.isfinite(no_micro):
        return None
    x1 = (yes_micro - mid) / spread
    x2 = ((1.0 - no_micro) - mid) / spread
    x3 = (yes_bid_depth - yes_ask_depth) / (yes_bid_depth + yes_ask_depth + 1e-9)
    x4 = (no_ask_depth - no_bid_depth) / (no_ask_depth + no_bid_depth + 1e-9)
    parity = (yes_micro - (1.0 - no_micro)) / spread
    return [
        1.0,
        max(-2.0, min(2.0, x1)),
        max(-2.0, min(2.0, x2)),
        max(-1.0, min(1.0, x3)),
        max(-1.0, min(1.0, x4)),
        max(-2.0, min(2.0, parity)),
    ], mid, spread
