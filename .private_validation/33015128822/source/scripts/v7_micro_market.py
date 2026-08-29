#!/usr/bin/env python3
from __future__ import annotations

import csv
import json
import math
import os
import urllib.parse
from pathlib import Path
from typing import Any

from v7_market_common import TapeFlow, fee_per_share, finite, parse_array, request_json, resolve_fee_details


def atomic_json(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + f".tmp.{os.getpid()}")
    tmp.write_text(json.dumps(value, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    os.replace(tmp, path)


def atomic_csv(path: Path, fields: list[str], rows: list[dict[str, Any]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + f".tmp.{os.getpid()}")
    with tmp.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=fields)
        writer.writeheader()
        writer.writerows([{key: row.get(key, "") for key in fields} for row in rows])
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
        outcomes = [str(x).strip().lower() for x in parse_array(raw.get("outcomes"))]
        if len(ids) < 2:
            raise ValueError("missing tokens")
        yi, ni = 0, 1
        for i, name in enumerate(outcomes[: len(ids)]):
            if name == "yes":
                yi = i
            elif name == "no":
                ni = i
        self.raw = raw
        self.id = str(raw.get("id") or "")
        self.condition = str(raw.get("conditionId") or "")
        self.event = str(raw.get("eventId") or self.condition or self.id)
        events = raw.get("events")
        if isinstance(events, list) and events and isinstance(events[0], dict):
            self.event = str(events[0].get("id") or self.event)
        self.slug = str(raw.get("slug") or self.id)
        self.yes = ids[yi]
        self.no = ids[ni]
        self.liquidity = max(0.0, finite(raw.get("liquidityNum"), finite(raw.get("liquidity"), 0.0)))


class Book:
    def __init__(self, raw: dict[str, Any]):
        self.token = str(raw.get("asset_id") or "")
        self.tick = max(1e-6, finite(raw.get("tick_size"), 0.01))
        self.min_order = max(1.0, finite(raw.get("min_order_size"), 1.0))
        self.bids: list[tuple[float, float]] = []
        self.asks: list[tuple[float, float]] = []
        for key, values in (("bids", self.bids), ("asks", self.asks)):
            for row in raw.get(key, []):
                if not isinstance(row, dict):
                    continue
                price, size = finite(row.get("price")), finite(row.get("size"), 0.0)
                if math.isfinite(price) and 0 < price < 1 and size > 0:
                    values.append((price, size))
        self.bids.sort(reverse=True)
        self.asks.sort()

    @property
    def bid(self) -> float:
        return self.bids[0][0] if self.bids else math.nan

    @property
    def ask(self) -> float:
        return self.asks[0][0] if self.asks else math.nan

    @property
    def spread(self) -> float:
        return self.ask - self.bid if math.isfinite(self.ask) and math.isfinite(self.bid) else math.nan

    @property
    def mid(self) -> float:
        return 0.5 * (self.ask + self.bid) if math.isfinite(self.ask) and math.isfinite(self.bid) else math.nan

    def touch_size(self, bid_side: bool) -> float:
        levels = self.bids if bid_side else self.asks
        if not levels:
            return 0.0
        best = levels[0][0]
        return sum(size for price, size in levels if abs(price - best) <= 1e-12)

    def depth(self, bid_side: bool, n: int = 5) -> float:
        levels = self.bids if bid_side else self.asks
        if not levels:
            return 0.0
        best = levels[0][0]
        scale = max(1e-4, 3 * self.tick)
        return sum(size * math.exp(-abs(price - best) / scale) for price, size in levels[:n])

    def micro(self) -> float:
        db, da = self.depth(True), self.depth(False)
        if not math.isfinite(self.bid) or not math.isfinite(self.ask):
            return math.nan
        return (self.ask * db + self.bid * da) / (db + da) if db + da > 1e-12 else self.mid


def discover(gamma: str, limit: int, min_liquidity: float) -> list[Market]:
    output: list[Market] = []
    offset = 0
    while len(output) < limit and offset < 5000:
        query = urllib.parse.urlencode({
            "active": "true",
            "closed": "false",
            "limit": 100,
            "offset": offset,
            "order": "liquidityNum",
            "ascending": "false",
        })
        root = request_json(gamma.rstrip("/") + "/markets?" + query)
        batch = root if isinstance(root, list) else root.get("markets", []) if isinstance(root, dict) else []
        if not batch:
            break
        for raw in batch:
            if not isinstance(raw, dict):
                continue
            try:
                market = Market(raw)
            except ValueError:
                continue
            if market.id and market.condition and market.liquidity >= min_liquidity:
                output.append(market)
            if len(output) >= limit:
                break
        if len(batch) < 100:
            break
        offset += 100
    return output


def fetch_books(clob: str, markets: list[Market]) -> dict[str, Book]:
    tokens = [token for market in markets for token in (market.yes, market.no)]
    output: dict[str, Book] = {}
    for i in range(0, len(tokens), 80):
        root = request_json(clob.rstrip("/") + "/books", [{"token_id": token} for token in tokens[i : i + 80]])
        for raw in root if isinstance(root, list) else []:
            if not isinstance(raw, dict):
                continue
            book = Book(raw)
            if book.token and book.bids and book.asks:
                output[book.token] = book
    return output


def micro_signal(yes: Book, no: Book) -> tuple[float, float]:
    mid = yes.mid
    if not math.isfinite(mid):
        return 0.5, 0.0
    y, n = yes.micro(), no.micro()
    dy = yes.depth(True) + yes.depth(False)
    dn = no.depth(True) + no.depth(False)
    wy = math.sqrt(max(0.0, dy)) / (1.0 + 20.0 * max(0.0, yes.spread))
    wn = math.sqrt(max(0.0, dn)) / (1.0 + 20.0 * max(0.0, no.spread))
    q = mid
    if math.isfinite(y) and math.isfinite(n) and wy + wn > 1e-12:
        q = (wy * y + wn * (1.0 - n)) / (wy + wn)
    elif math.isfinite(y):
        q = y
    parity = abs(y - (1.0 - n)) if math.isfinite(y) and math.isfinite(n) else 0.25
    liquidity_conf = (dy + dn) / (dy + dn + 200.0)
    spread_conf = math.exp(-5.0 * (yes.spread + no.spread))
    confidence = max(0.02, min(1.0, liquidity_conf * spread_conf * math.exp(-8.0 * parity)))
    return max(0.001, min(0.999, q)), confidence
