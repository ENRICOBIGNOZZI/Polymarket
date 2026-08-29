#!/usr/bin/env python3
from __future__ import annotations

import json
import math
import re
import statistics
import urllib.parse
import urllib.request
from collections import defaultdict
from dataclasses import dataclass
from typing import Any

from polymarket_fees import FeeDetails, parse_fee_details

THRESHOLD = re.compile(r"([$€£]?\s*\d[\d,]*(?:\.\d+)?\s*(?:k|m|b|%|bp|bps)?)", re.I)
DIRECTION = re.compile(r"\b(above|below|over|under|reach|exceed|dip|at least|at most|more than|less than)\b", re.I)


def finite(value: Any, default: float = math.nan) -> float:
    try:
        x = float(value)
    except (TypeError, ValueError, OverflowError):
        return default
    return x if math.isfinite(x) else default


def parse_array(value: Any) -> list[Any]:
    if isinstance(value, list):
        return value
    if isinstance(value, str):
        try:
            parsed = json.loads(value)
            return parsed if isinstance(parsed, list) else []
        except json.JSONDecodeError:
            return []
    return []


def request_json(url: str, payload: Any | None = None, timeout: int = 20) -> Any:
    data = None if payload is None else json.dumps(payload).encode("utf-8")
    request = urllib.request.Request(
        url,
        data=data,
        headers={"User-Agent": "polymarket-v7-paper/2", "Content-Type": "application/json"},
    )
    with urllib.request.urlopen(request, timeout=timeout) as response:
        return json.loads(response.read().decode("utf-8"))


def logit(probability: float) -> float:
    p = min(1.0 - 1e-6, max(1e-6, probability))
    return math.log(p / (1.0 - p))


@dataclass
class Market:
    market_id: str
    event_id: str
    question: str
    yes: str
    no: str
    liquidity: float
    condition_id: str
    fee: FeeDetails | None


@dataclass
class Book:
    bid: float
    ask: float
    bid_size: float
    ask_size: float
    min_order: float

    @property
    def mid(self) -> float:
        return 0.5 * (self.bid + self.ask)

    @property
    def spread(self) -> float:
        return max(0.0, self.ask - self.bid)


def parse_market(raw: dict[str, Any]) -> Market | None:
    ids = [str(value) for value in parse_array(raw.get("clobTokenIds"))]
    outcomes = [str(value).lower() for value in parse_array(raw.get("outcomes"))]
    if len(ids) < 2:
        return None
    yes_index, no_index = 0, 1
    for index, outcome in enumerate(outcomes[: len(ids)]):
        if outcome == "yes":
            yes_index = index
        elif outcome == "no":
            no_index = index
    market_id = str(raw.get("id") or "")
    question = str(raw.get("question") or "")
    if not market_id or not question:
        return None
    event_id = str(raw.get("eventId") or "")
    events = raw.get("events")
    if not event_id and isinstance(events, list) and events and isinstance(events[0], dict):
        event_id = str(events[0].get("id") or "")
    condition_id = str(raw.get("conditionId") or "")
    return Market(
        market_id=market_id,
        event_id=event_id or condition_id or market_id,
        question=question,
        yes=ids[yes_index],
        no=ids[no_index],
        liquidity=max(0.0, finite(raw.get("liquidityNum"), 0.0)),
        condition_id=condition_id,
        fee=parse_fee_details(raw),
    )


def discover(gamma: str, limit: int, min_liquidity: float) -> list[Market]:
    out: list[Market] = []
    offset = 0
    while len(out) < limit and offset < 5000:
        params = urllib.parse.urlencode({
            "active": "true",
            "closed": "false",
            "limit": 100,
            "offset": offset,
            "order": "liquidityNum",
            "ascending": "false",
        })
        raw = request_json(f"{gamma.rstrip('/')}/markets?{params}")
        batch = raw if isinstance(raw, list) else raw.get("markets", []) if isinstance(raw, dict) else []
        if not batch:
            break
        for row in batch:
            market = parse_market(row) if isinstance(row, dict) else None
            if market and market.liquidity >= min_liquidity:
                out.append(market)
            if len(out) >= limit:
                break
        if len(batch) < 100:
            break
        offset += 100
    return out


def payoff_family(question: str) -> str | None:
    if not THRESHOLD.search(question) or not DIRECTION.search(question):
        return None
    value = question.lower()
    value = THRESHOLD.sub(" <threshold> ", value)
    value = DIRECTION.sub(" <direction> ", value)
    value = re.sub(r"\b20\d{2}\b", " <year> ", value)
    value = re.sub(r"[^a-z<>]+", " ", value)
    return re.sub(r"\s+", " ", value).strip()


def clusters(markets: list[Market], max_clusters: int) -> list[tuple[str, list[Market]]]:
    groups: dict[str, list[Market]] = defaultdict(list)
    for market in markets:
        groups["event:" + market.event_id].append(market)
        family = payoff_family(market.question)
        if family:
            groups["payoff:" + family].append(market)
    candidates = [(key, values) for key, values in groups.items() if 3 <= len(values) <= 25]
    candidates.sort(key=lambda item: sum(market.liquidity for market in item[1]), reverse=True)
    seen: set[tuple[str, ...]] = set()
    out: list[tuple[str, list[Market]]] = []
    for key, values in candidates:
        ids = tuple(sorted(market.market_id for market in values))
        if ids in seen:
            continue
        seen.add(ids)
        out.append((key, values))
        if len(out) >= max_clusters:
            break
    return out


def fetch_books(clob: str, markets: list[Market]) -> dict[str, Book]:
    tokens = [token for market in markets for token in (market.yes, market.no)]
    out: dict[str, Book] = {}
    for index in range(0, len(tokens), 80):
        raw = request_json(clob.rstrip("/") + "/books", [{"token_id": token} for token in tokens[index : index + 80]])
        for row in raw if isinstance(raw, list) else []:
            if not isinstance(row, dict):
                continue
            token = str(row.get("asset_id") or "")
            bids: list[tuple[float, float]] = []
            asks: list[tuple[float, float]] = []
            for item in row.get("bids", []):
                if isinstance(item, dict):
                    price, size = finite(item.get("price")), finite(item.get("size"), 0.0)
                    if math.isfinite(price) and 0 < price < 1 and size > 0:
                        bids.append((price, size))
            for item in row.get("asks", []):
                if isinstance(item, dict):
                    price, size = finite(item.get("price")), finite(item.get("size"), 0.0)
                    if math.isfinite(price) and 0 < price < 1 and size > 0:
                        asks.append((price, size))
            if token and bids and asks:
                bids.sort(reverse=True)
                asks.sort()
                out[token] = Book(
                    bids[0][0],
                    asks[0][0],
                    bids[0][1],
                    asks[0][1],
                    max(1.0, finite(row.get("min_order_size"), 1.0)),
                )
    return out


def parse_history(rows: list[Any], fidelity_minutes: int) -> dict[int, float]:
    bucket = fidelity_minutes * 60
    out: dict[int, float] = {}
    for row in rows:
        if not isinstance(row, dict):
            continue
        timestamp = int(finite(row.get("t"), 0.0))
        price = finite(row.get("p"))
        if timestamp > 0 and math.isfinite(price) and 0 < price < 1:
            out[(timestamp // bucket) * bucket] = logit(price)
    return out


def fetch_histories(
    clob: str,
    token_by_market: dict[str, str],
    start: int,
    end: int,
    fidelity_minutes: int,
) -> tuple[dict[str, dict[int, float]], list[str]]:
    token_to_market = {token: market_id for market_id, token in token_by_market.items()}
    tokens = list(token_to_market)
    out: dict[str, dict[int, float]] = {}
    failures: list[str] = []
    for index in range(0, len(tokens), 20):
        batch = tokens[index : index + 20]
        try:
            raw = request_json(clob.rstrip("/") + "/batch-prices-history", {
                "markets": batch,
                "start_ts": start,
                "end_ts": end,
                "fidelity": fidelity_minutes,
            })
            history = raw.get("history", {}) if isinstance(raw, dict) else {}
            if isinstance(history, dict):
                for token, rows in history.items():
                    if token in token_to_market and isinstance(rows, list):
                        parsed = parse_history(rows, fidelity_minutes)
                        if parsed:
                            out[token_to_market[token]] = parsed
        except Exception as exc:
            failures.append(f"batch:{type(exc).__name__}")
    missing = [(market_id, token) for market_id, token in token_by_market.items() if market_id not in out][:20]
    for market_id, token in missing:
        try:
            url = (
                f"{clob.rstrip('/')}/prices-history?market={urllib.parse.quote(token)}"
                f"&startTs={start}&endTs={end}&fidelity={fidelity_minutes}"
            )
            raw = request_json(url)
            parsed = parse_history(raw.get("history", []) if isinstance(raw, dict) else [], fidelity_minutes)
            if parsed:
                out[market_id] = parsed
        except Exception as exc:
            failures.append(f"single:{market_id}:{type(exc).__name__}")
    return out, failures
