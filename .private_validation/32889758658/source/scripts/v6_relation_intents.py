#!/usr/bin/env python3
from __future__ import annotations

import argparse
import csv
import datetime as dt
import io
import json
import math
import os
import re
import threading
import time
import urllib.parse
import urllib.request
from dataclasses import dataclass
from pathlib import Path
from typing import Any

FIELDS = [
    "bundle_id", "strategy", "event_id", "created_ts", "mode", "expected_edge",
    "max_notional", "market_id", "side", "weight", "limit_price",
    "execution_deadline_ts", "hold_deadline_ts",
]

UP = re.compile(r"\b(above|over|exceed|exceeds|reach|reaches|at least|more than|higher than)\b", re.I)
DOWN = re.compile(r"\b(below|under|dip to|fall to|at most|less than|lower than)\b", re.I)
NUMBER = re.compile(r"(?P<prefix>[$€£]?)\s*(?P<num>\d[\d,]*(?:\.\d+)?)\s*(?P<suffix>k|m|b|%|bp|bps)?", re.I)
DATE_TOKEN = re.compile(r"\b(?:jan(?:uary)?|feb(?:ruary)?|mar(?:ch)?|apr(?:il)?|may|jun(?:e)?|jul(?:y)?|aug(?:ust)?|sep(?:tember)?|oct(?:ober)?|nov(?:ember)?|dec(?:ember)?|20\d{2})\b", re.I)


def finite(value: Any, default: float = math.nan) -> float:
    try:
        out = float(value)
    except (TypeError, ValueError, OverflowError):
        return default
    return out if math.isfinite(out) else default


def parse_ts(value: Any) -> int:
    if isinstance(value, (int, float)):
        x = int(value)
        return x // 1000 if x > 10_000_000_000 else x
    text = str(value or "").strip()
    if not text:
        return 0
    try:
        x = int(float(text))
        return x // 1000 if x > 10_000_000_000 else x
    except ValueError:
        pass
    try:
        parsed = dt.datetime.fromisoformat(text.replace("Z", "+00:00"))
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=dt.timezone.utc)
        return int(parsed.timestamp())
    except ValueError:
        return 0


def request_json(url: str, payload: Any | None = None, timeout: int = 20) -> Any:
    data = None if payload is None else json.dumps(payload).encode("utf-8")
    req = urllib.request.Request(url, data=data, headers={"User-Agent": "polymarket-v6-paper/1", "Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=timeout) as response:
        return json.loads(response.read().decode("utf-8"))


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


@dataclass
class Market:
    market_id: str
    condition_id: str
    event_id: str
    question: str
    yes_token: str
    no_token: str
    liquidity: float
    neg_risk: bool
    end_ts: int


@dataclass
class Book:
    bid: float
    ask: float
    bid_size: float
    ask_size: float
    min_order: float


def parse_market(raw: dict[str, Any]) -> Market | None:
    ids = [str(x) for x in parse_array(raw.get("clobTokenIds"))]
    outcomes = [str(x).strip().lower() for x in parse_array(raw.get("outcomes"))]
    if len(ids) < 2:
        return None
    yi, ni = 0, 1
    for i, name in enumerate(outcomes[:len(ids)]):
        if name == "yes": yi = i
        elif name == "no": ni = i
    mid = str(raw.get("id") or "").strip()
    condition = str(raw.get("conditionId") or "").strip()
    question = str(raw.get("question") or "").strip()
    if not mid or not condition or not question:
        return None
    event_id = str(raw.get("eventId") or "").strip()
    events = raw.get("events")
    if not event_id and isinstance(events, list) and events and isinstance(events[0], dict):
        event_id = str(events[0].get("id") or "")
    return Market(
        mid, condition, event_id or condition, question, ids[yi], ids[ni],
        max(0.0, finite(raw.get("liquidityNum"), finite(raw.get("liquidity"), 0.0))),
        bool(raw.get("negRisk", False)), parse_ts(raw.get("endDate") or raw.get("endDateIso")),
    )


def discover(gamma: str, limit: int, min_liquidity: float) -> list[Market]:
    out: list[Market] = []
    offset = 0
    while len(out) < limit and offset < 5000:
        params = urllib.parse.urlencode({"active": "true", "closed": "false", "limit": 100, "offset": offset, "order": "liquidityNum", "ascending": "false"})
        raw = request_json(f"{gamma}/markets?{params}")
        batch = raw if isinstance(raw, list) else raw.get("markets", []) if isinstance(raw, dict) else []
        if not batch:
            break
        for item in batch:
            if not isinstance(item, dict):
                continue
            m = parse_market(item)
            if m and m.liquidity >= min_liquidity:
                out.append(m)
                if len(out) >= limit:
                    break
        if len(batch) < 100:
            break
        offset += 100
    return out


def fetch_books(clob: str, tokens: list[str]) -> dict[str, Book]:
    out: dict[str, Book] = {}
    for start in range(0, len(tokens), 80):
        batch = tokens[start:start + 80]
        rows = request_json(f"{clob}/books", [{"token_id": x} for x in batch])
        if not isinstance(rows, list):
            continue
        for raw in rows:
            if not isinstance(raw, dict):
                continue
            token = str(raw.get("asset_id") or "")
            bids = sorted(((finite(x.get("price")), finite(x.get("size"), 0.0)) for x in raw.get("bids", []) if isinstance(x, dict)), reverse=True)
            asks = sorted((finite(x.get("price")), finite(x.get("size"), 0.0)) for x in raw.get("asks", []) if isinstance(x, dict))
            bids = [(p, q) for p, q in bids if math.isfinite(p) and 0 < p < 1 and q > 0]
            asks = [(p, q) for p, q in asks if math.isfinite(p) and 0 < p < 1 and q > 0]
            if token and bids and asks:
                out[token] = Book(bids[0][0], asks[0][0], bids[0][1], asks[0][1], max(1.0, finite(raw.get("min_order_size"), 1.0)))
    return out


def number_value(match: re.Match[str]) -> float:
    x = float(match.group("num").replace(",", ""))
    suffix = (match.group("suffix") or "").lower()
    if suffix == "k": x *= 1e3
    elif suffix == "m": x *= 1e6
    elif suffix == "b": x *= 1e9
    elif suffix == "%": x /= 100.0
    elif suffix in {"bp", "bps"}: x /= 10000.0
    return x


def threshold_signature(question: str) -> tuple[str, str, float] | None:
    direction = "UP" if UP.search(question) else "DOWN" if DOWN.search(question) else ""
    if not direction:
        return None
    matches = list(NUMBER.finditer(question))
    if not matches:
        return None
    # Prefer a money/percent/suffixed threshold; otherwise use the largest numeric
    # token, which avoids treating the year as the threshold in most contracts.
    ranked = sorted(matches, key=lambda m: (bool(m.group("prefix") or m.group("suffix")), number_value(m)), reverse=True)
    chosen = ranked[0]
    threshold = number_value(chosen)
    text = question.lower()
    a, b = chosen.span()
    text = text[:a] + " <threshold> " + text[b:]
    text = UP.sub(" <direction> ", text)
    text = DOWN.sub(" <direction> ", text)
    text = re.sub(r"\b\d{4}\b", " <year> ", text)
    text = re.sub(r"[^a-z<>%]+", " ", text)
    date_bits = " ".join(x.group(0).lower() for x in DATE_TOKEN.finditer(question))
    family = re.sub(r"\s+", " ", text).strip() + "|" + date_bits
    return family, direction, threshold


def maker_bundle(now: int, strategy: str, event_id: str, legs: list[tuple[Market, str, Book]], min_edge: float, max_trade: float, serial: int) -> list[dict[str, Any]]:
    if len(legs) < 2:
        return []
    cost = sum(book.bid for _, _, book in legs)
    edge = 1.0 - cost
    if edge <= min_edge:
        return []
    min_shares = min(book.bid_size for _, _, book in legs)
    minimum = max(book.min_order for _, _, book in legs)
    if min_shares + 1e-12 < minimum:
        return []
    max_notional = min(max_trade, min_shares * max(cost, 1e-6))
    if max_notional <= 0:
        return []
    end_ts = max((m.end_ts for m, _, _ in legs), default=0)
    hold = max(now + 3600, end_ts + 3600 if end_ts else now + 7 * 86400)
    execution = now + 180
    bundle = f"{strategy}-{now}-{serial}"
    rows = []
    for market, side, book in legs:
        rows.append({
            "bundle_id": bundle, "strategy": strategy, "event_id": event_id,
            "created_ts": now, "mode": "MAKER", "expected_edge": edge,
            "max_notional": max_notional, "market_id": market.market_id,
            "side": side, "weight": 1.0, "limit_price": book.bid,
            "execution_deadline_ts": execution, "hold_deadline_ts": hold,
        })
    return rows


def graph_intents(gamma: str, clob: str, markets: list[Market], books: dict[str, Book], now: int, min_edge: float, max_trade: float, max_events: int) -> tuple[list[dict[str, Any]], dict[str, int]]:
    event_ids: list[str] = []
    for m in markets:
        if m.neg_risk and m.event_id not in event_ids:
            event_ids.append(m.event_id)
    rows: list[dict[str, Any]] = []
    stats = {"events_considered": 0, "events_complete": 0, "bundles": 0}
    serial = 0
    for event_id in event_ids[:max_events]:
        stats["events_considered"] += 1
        try:
            event = request_json(f"{gamma}/events/{event_id}")
        except Exception:
            continue
        if not isinstance(event, dict) or not event.get("negRisk") or event.get("negRiskAugmented"):
            continue
        raw_markets = event.get("markets")
        if not isinstance(raw_markets, list) or len(raw_markets) < 2:
            continue
        full = [parse_market(x) for x in raw_markets if isinstance(x, dict)]
        if len(full) != len(raw_markets) or any(x is None for x in full):
            continue
        full = [x for x in full if x is not None]
        missing = [m.yes_token for m in full if m.yes_token not in books]
        if missing:
            try:
                books.update(fetch_books(clob, missing))
            except Exception:
                continue
        if any(m.yes_token not in books for m in full):
            continue
        stats["events_complete"] += 1
        legs = [(m, "YES", books[m.yes_token]) for m in full]
        bundle = maker_bundle(now, "GRAPH_HARD", event_id, legs, min_edge, max_trade, serial)
        if bundle:
            rows.extend(bundle); stats["bundles"] += 1; serial += 1
    return rows, stats


def structural_intents(markets: list[Market], books: dict[str, Book], now: int, min_edge: float, max_trade: float) -> tuple[list[dict[str, Any]], dict[str, int]]:
    families: dict[tuple[str, str], list[tuple[float, Market]]] = {}
    for m in markets:
        sig = threshold_signature(m.question)
        if sig:
            family, direction, threshold = sig
            families.setdefault((family, direction), []).append((threshold, m))
    rows: list[dict[str, Any]] = []
    serial = 0
    candidates = 0
    for (family, direction), values in families.items():
        values.sort(key=lambda x: x[0])
        for (lo_t, lo), (hi_t, hi) in zip(values, values[1:]):
            if not hi_t > lo_t:
                continue
            candidates += 1
            if direction == "UP":
                # high => low. YES(low) + NO(high) has minimum terminal payoff 1.
                token_legs = [(lo, "YES", lo.yes_token), (hi, "NO", hi.no_token)]
            else:
                # low => high for DOWN events. YES(high) + NO(low) has minimum payoff 1.
                token_legs = [(hi, "YES", hi.yes_token), (lo, "NO", lo.no_token)]
            if any(token not in books for _, _, token in token_legs):
                continue
            legs = [(m, side, books[token]) for m, side, token in token_legs]
            event_id = "STRUCT:" + str(abs(hash((family, direction))))
            bundle = maker_bundle(now, "STRUCTURAL", event_id, legs, min_edge, max_trade, serial)
            if bundle:
                rows.extend(bundle); serial += 1
    return rows, {"families": len(families), "relations_considered": candidates, "bundles": serial}


def atomic_csv(path: Path, rows: list[dict[str, Any]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + f".tmp.{os.getpid()}.{threading.get_ident()}")
    with tmp.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=FIELDS)
        writer.writeheader(); writer.writerows(rows)
    os.replace(tmp, path)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--config", type=Path, default=Path("config/paper_v6.json"))
    ap.add_argument("--output", type=Path, required=True)
    ap.add_argument("--status", type=Path, required=True)
    ap.add_argument("--markets", type=int, default=700)
    ap.add_argument("--min-liquidity", type=float, default=10.0)
    ap.add_argument("--min-edge", type=float, default=0.0002)
    ap.add_argument("--max-trade-usd", type=float, default=60.0)
    ap.add_argument("--max-events", type=int, default=80)
    args = ap.parse_args()
    cfg = json.loads(args.config.read_text(encoding="utf-8"))
    gamma, clob = cfg["gamma_url"], cfg["clob_url"]
    now = int(time.time())
    failures: list[str] = []
    try:
        markets = discover(gamma, args.markets, args.min_liquidity)
        tokens = [token for m in markets for token in (m.yes_token, m.no_token)]
        books = fetch_books(clob, tokens)
    except Exception as exc:
        markets, books = [], {}
        failures.append(f"market_data:{type(exc).__name__}:{exc}")
    structural, s_stats = structural_intents(markets, books, now, args.min_edge, args.max_trade_usd)
    try:
        graph, g_stats = graph_intents(gamma, clob, markets, books, now, args.min_edge, args.max_trade_usd, args.max_events)
    except Exception as exc:
        graph, g_stats = [], {"events_considered": 0, "events_complete": 0, "bundles": 0}
        failures.append(f"graph:{type(exc).__name__}:{exc}")
    rows = structural + graph
    rows.sort(key=lambda x: (float(x["expected_edge"]), x["bundle_id"]), reverse=True)
    atomic_csv(args.output, rows)
    status = {
        "timestamp": now, "paper_only": True, "markets": len(markets), "books": len(books),
        "structural": s_stats, "graph_hard": g_stats,
        "intent_rows": len(rows), "bundles": len({r["bundle_id"] for r in rows}),
        "best_edge": max((float(r["expected_edge"]) for r in rows), default=0.0),
        "failures": failures,
    }
    tmp = args.status.with_name(
        args.status.name + f".tmp.{os.getpid()}.{threading.get_ident()}"
    )
    tmp.parent.mkdir(parents=True, exist_ok=True)
    tmp.write_text(json.dumps(status, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    os.replace(tmp, args.status)
    print(json.dumps(status, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
