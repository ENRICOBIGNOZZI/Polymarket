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
from v7_micro_target import label_matured_horizon_probes, label_matured_samples
from v7_shared_market_state import SharedStateError, load_snapshot, synchronized_books


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
        self.exchange_ts_ms = self.exchange_ts * 1000
        self.received_ts_ms = self.received_ts * 1000
        self.source_received_ts = self.received_ts
        self.source_received_ts_ms = self.received_ts_ms
        self.snapshot_published_ts = self.received_ts
        self.snapshot_published_ts_ms = self.received_ts_ms
        self.state_version = 0
        self.lineage_epoch = 0
        self.economic_novelty = False
        self.last_book_change_receive_ms = self.received_ts_ms
        self.last_trade_receive_ms = 0
        self.last_trade_exchange_ms = 0
        self.snapshot_id = str(raw.get("hash") or "")
        self.lineage_continuous = False
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


def fetch_shared_books(path: Path, markets: list[Market], *, model_sha: str,
                       max_publish_age_ms: int = 2500) -> dict[str, Book]:
    snapshot = load_snapshot(
        path, expected_sha=model_sha, max_publish_age_ms=max_publish_age_ms,
    )
    # Coverage is market-local. The shared bus can legitimately contain a
    # bounded subset of the larger Gamma discovery universe; one uncovered
    # token must not discard every otherwise atomic YES/NO pair. Each pair is
    # still validated together against the same bus snapshot and continuous
    # lineage before it becomes visible to the strategy.
    selected: dict[str, dict[str, Any]] = {}
    for market in markets:
        try:
            pair = synchronized_books(
                snapshot, (market.yes, market.no), require_continuous=True,
            )
        except SharedStateError:
            continue
        selected.update(pair)
    if markets and not selected:
        raise SharedStateError("bundle:no_complete_markets")
    output: dict[str, Book] = {}
    for token, raw in selected.items():
        output[token] = _shared_book(token, raw)
    return output


def _shared_book(token: str, raw: dict[str, Any]) -> Book:
    book = Book({
        "asset_id": token,
        "timestamp": int(raw["exchange_ts_ms"]),
        "tick_size": raw["tick_size"],
        "min_order_size": raw["min_order"],
        "bids": [{"price": price, "size": size} for price, size in raw["bids"]],
        "asks": [{"price": price, "size": size} for price, size in raw["asks"]],
        "hash": raw["bus_snapshot_id"],
    }, received_ts=int(raw["received_ms"]) / 1000.0)
    book.exchange_ts_ms = int(raw["exchange_ts_ms"])
    book.received_ts_ms = int(raw["received_ms"])
    book.snapshot_published_ts_ms = int(
        raw.get("snapshot_published_ms") or raw["received_ms"])
    book.snapshot_published_ts = book.snapshot_published_ts_ms // 1000
    book.source_received_ts_ms = int(
        raw.get("source_receive_ts_ms") or raw["received_ms"])
    book.source_received_ts = book.source_received_ts_ms // 1000
    book.state_version = int(raw.get("state_version") or 0)
    book.lineage_epoch = int(raw.get("lineage_epoch") or 0)
    book.economic_novelty = raw.get("economic_novelty") is True
    book.last_book_change_receive_ms = int(
        raw.get("last_book_change_receive_ms") or book.source_received_ts_ms)
    book.last_trade_receive_ms = int(raw.get("last_trade_receive_ms") or 0)
    book.last_trade_exchange_ms = int(raw.get("last_trade_exchange_ms") or 0)
    book.snapshot_id = str(raw["bus_snapshot_id"])
    book.lineage_continuous = True
    return book


def fetch_shared_market_bundle(
    path: Path, *, model_sha: str, market_limit: int,
    min_visible_liquidity: float, max_publish_age_ms: int = 2500,
) -> tuple[list[Market], dict[str, Book]]:
    """Build fee-attested market pairs directly from the canonical WS image.

    The shared producer already publishes exact market/event/outcome identity,
    full books and the authoritative fee registry. Re-querying Gamma and the
    CLOB for the same metadata made a 30-second target depend on a 20-second
    network timeout and prevented prospective labels from ever maturing.
    """
    snapshot = load_snapshot(
        path, expected_sha=model_sha, max_publish_age_ms=max_publish_age_ms)
    grouped: dict[str, dict[str, dict[str, Any]]] = {}
    for raw in snapshot["books"].values():
        market_id = str(raw.get("market_id") or "")
        outcome = str(raw.get("outcome") or "").upper()
        if market_id and outcome in {"YES", "NO"}:
            grouped.setdefault(market_id, {})[outcome] = raw

    candidates: list[tuple[float, str, dict[str, Any], dict[str, Any]]] = []
    for market_id in grouped:
        sides = grouped[market_id]
        if set(sides) != {"YES", "NO"}:
            continue
        yes, no = sides["YES"], sides["NO"]
        yes_token, no_token = str(yes.get("token") or ""), str(no.get("token") or "")
        condition_id = str(yes.get("condition_id") or "")
        event_id = str(yes.get("event_id") or "")
        if (
            yes.get("lineage_continuous") is not True
            or no.get("lineage_continuous") is not True
            or yes.get("fee_verified") is not True
            or no.get("fee_verified") is not True
            or not yes_token or not no_token or yes_token == no_token
            or not condition_id or condition_id != str(no.get("condition_id") or "")
            or not event_id or event_id != str(no.get("event_id") or "")
            or not str(yes.get("bus_snapshot_id") or "")
            or str(yes.get("bus_snapshot_id") or "")
                != str(no.get("bus_snapshot_id") or "")
        ):
            continue
        fee_tuple = (
            float(yes["fee_rate"]), float(yes["fee_exponent"]),
            bool(yes["fee_taker_only"]),
        )
        no_fee_tuple = (
            float(no["fee_rate"]), float(no["fee_exponent"]),
            bool(no["fee_taker_only"]),
        )
        if fee_tuple != no_fee_tuple:
            continue
        visible_liquidity = sum(
            size for raw in (yes, no)
            for side in ("bids", "asks") for _price, size in raw[side]
        )
        if visible_liquidity < max(0.0, float(min_visible_liquidity)):
            continue
        candidates.append((visible_liquidity, market_id, yes, no))

    # Gamma discovery was liquidity-ranked. Preserve that useful admission
    # property when the canonical snapshot becomes the sole data source.
    candidates.sort(key=lambda row: (-row[0], row[1]))
    markets: list[Market] = []
    books: dict[str, Book] = {}
    limit = max(0, int(market_limit))
    for visible_liquidity, market_id, yes, no in candidates:
        raw_market = {
            "id": market_id,
            "conditionId": str(yes.get("condition_id") or ""),
            "eventId": str(yes.get("event_id") or ""),
            "slug": market_id,
            "clobTokenIds": [yes["token"], no["token"]],
            "outcomes": ["Yes", "No"],
            "liquidityNum": visible_liquidity,
        }
        try:
            market = Market(raw_market)
        except ValueError:
            continue
        market.fee = FeeDetails(
            enabled=fee_tuple[0] > 0.0,
            rate=fee_tuple[0], exponent=fee_tuple[1],
            taker_only=fee_tuple[2], source="shared_state:verified_fee_registry",
        )
        markets.append(market)
        books[market.yes] = _shared_book(market.yes, yes)
        books[market.no] = _shared_book(market.no, no)
        if limit and len(markets) >= limit:
            break
    if not markets:
        raise SharedStateError("bundle:no_complete_fee_verified_markets")
    return markets, books


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
